# -*- coding: utf-8 -*-
from __future__ import unicode_literals
import codecs
import json
import os
import pkg_resources
import re
import redis
import requests
import shutil
import six
import tempfile
import threading
import time

from pathlib import Path
from six.moves import shlex_quote as quote
from six.moves.urllib.parse import urlparse
from six.moves.urllib.parse import urlunparse
from unicodedata import normalize

from dallinger import data
from dallinger import db
from dallinger import heroku
from dallinger import recruiters
from dallinger import registration
from dallinger.config import get_config
from dallinger.heroku.tools import HerokuApp
from dallinger.heroku.tools import HerokuLocalWrapper
from dallinger.utils import connect_to_redis
from dallinger.utils import dallinger_package_path
from dallinger.utils import ensure_directory
from dallinger.utils import get_base_url
from dallinger.utils import open_browser
from dallinger.utils import GitClient
from faker import Faker


fake = Faker()


def exclusion_policy():
    """Returns a callable which, when passed a directory path and a list
    of files in that directory, will return a subset of the files which should
    be excluded from a copy or some other action.

    See https://docs.python.org/3/library/shutil.html#shutil.ignore_patterns
    """
    patterns = set(
        [
            ".git",
            "config.txt",
            "*.db",
            "*.dmg",
            "node_modules",
            "snapshots",
            "data",
            "server.log",
            "__pycache__",
        ]
    )

    return shutil.ignore_patterns(*patterns)


class ExperimentFileSource(object):
    """Treat an experiment directory as a potential source of files for
    copying to a temp directory as part of a deployment (debug or otherwise).
    """

    def __init__(self, root_dir="."):
        self.root = root_dir
        self.git = GitClient()

    @property
    def files(self):
        """A Set of all files copyable in the source directory, accounting for
        exclusions.
        """
        return set(self._walk())

    @property
    def size(self):
        """Combined size of all files, accounting for exclusions."""
        return sum([os.path.getsize(path) for path in self._walk()])

    def selective_copy_to(self, destination):
        """Write files from the source directory to another directory, skipping
        files excluded by the general exclusion_policy, plus any files
        ignored by git configuration.
        """
        for path in self.files:
            subpath = os.path.relpath(path, start=self.root)
            target_folder = os.path.join(destination, os.path.dirname(subpath))
            ensure_directory(target_folder)
            shutil.copy2(path, target_folder)

    def _walk(self):
        # The GitClient and os.walk may return different representations of the
        # same unicode characters, so we use unicodedata.normalize() for
        # comparisons:
        # list(name_from_git)
        # ['å', ' ', 'f', 'i', 'l', 'e', '.', 't', 'x', 't']
        # list(from_os_walk)
        # ['a', '̊', ' ', 'f', 'i', 'l', 'e', '.', 't', 'x', 't']
        exclusions = exclusion_policy()
        git_files = {
            os.path.join(self.root, normalize("NFC", f)) for f in self.git.files()
        }
        for dirpath, dirnames, filenames in os.walk(self.root, topdown=True):
            current_exclusions = exclusions(dirpath, os.listdir(dirpath))
            # Modifying dirnames in-place will prune the subsequent files and
            # directories visited by os.walk. This is only possible when
            # topdown = True
            dirnames[:] = [d for d in dirnames if d not in current_exclusions]
            legit_files = {
                os.path.join(dirpath, f)
                for f in filenames
                if f not in current_exclusions
            }
            if git_files:
                normalized = {
                    normalize("NFC", six.text_type(f)): f for f in legit_files
                }
                legit_files = {v for k, v in normalized.items() if k in git_files}
            for legit in legit_files:
                yield legit


class ExplicitFileSource(object):
    """Add files that are explicitly requested by the experimenter with a hook function."""

    def __init__(self, root_dir="."):
        self.root = root_dir

    def _get_mapping(self, dst):
        from dallinger.config import initialize_experiment_package

        initialize_experiment_package(dst)
        from dallinger.experiment import load

        exp_class = load()
        extra_files = getattr(exp_class, "extra_files", None)
        if extra_files is None:
            try:
                from dallinger_experiment.experiment import extra_files
            except ImportError:
                try:
                    from dallinger_experiment.dallinger_experiment import extra_files
                except ImportError:
                    pass

        if extra_files is not None:
            for src, filename in extra_files():
                filename = filename.lstrip("/")
                if os.path.isdir(src):
                    for dirpath, dirnames, filenames in os.walk(src, topdown=True):
                        for fn in filenames:
                            dst_fileparts = [dst, filename] + dirnames + [fn]
                            dst_filepath = os.path.join(*dst_fileparts)
                            yield (
                                os.path.join(dirpath, fn),
                                dst_filepath,
                            )
                else:
                    dst_filepath = os.path.join(dst, filename)
                    yield (src, dst_filepath)

    @property
    def files(self):
        """A Set of all files copyable in the source directory, accounting for
        exclusions.
        """
        return {src for (src, dst) in self._get_mapping("")}

    @property
    def size(self):
        """Combined size of all files, accounting for exclusions."""
        return sum([os.path.getsize(path) for path in self.files])

    def selective_copy_to(self, destination):
        """Write files declared in extra_files from the source directory
        to another directory.
        """
        for from_path, to_path in self._get_mapping(destination):
            target_folder = os.path.dirname(to_path)
            ensure_directory(target_folder)
            shutil.copyfile(from_path, to_path)


def assemble_experiment_temp_dir(config):
    """Create a temp directory from which to run an experiment.
    The new directory will include:
    - Copies of custom experiment files which don't match the exclusion policy
    - Templates and static resources from Dallinger
    - An export of the loaded configuration
    - Heroku-specific files (Procile, runtime.txt) from Dallinger
    - A requirements.txt file with the contents from the constraints.txt file
      in the experiment (Dallinger should have generated one with pip-compile
      if needed by the time we reach this code)

    Assumes the experiment root directory is the current working directory.

    Returns the absolute path of the new directory.
    """
    exp_id = config.get("id")
    dst = os.path.join(tempfile.mkdtemp(), exp_id)

    # Copy local experiment files, minus some
    ExperimentFileSource(os.getcwd()).selective_copy_to(dst)

    # Export the loaded configuration
    config.write(filter_sensitive=True, directory=dst)

    # Save the experiment id
    with open(os.path.join(dst, "experiment_id.txt"), "w") as file:
        file.write(exp_id)

    # Copy Dallinger files
    dallinger_root = dallinger_package_path()
    ensure_directory(os.path.join(dst, "static", "scripts"))
    ensure_directory(os.path.join(dst, "static", "css"))
    frontend_files = [
        os.path.join("static", "css", "bootstrap.min.css"),
        os.path.join("static", "css", "dallinger.css"),
        os.path.join("static", "css", "dashboard.css"),
        os.path.join("static", "scripts", "jquery-3.5.1.min.js"),
        os.path.join("static", "scripts", "popper.min.js"),
        os.path.join("static", "scripts", "bootstrap.min.js"),
        os.path.join("static", "scripts", "clipboard.min.js"),
        os.path.join("static", "scripts", "dallinger2.js"),
        os.path.join("static", "scripts", "network-monitor.js"),
        os.path.join("static", "scripts", "reqwest.min.js"),
        os.path.join("static", "scripts", "require.js"),
        os.path.join("static", "scripts", "reconnecting-websocket.js"),
        os.path.join("static", "scripts", "spin.min.js"),
        os.path.join("static", "scripts", "tracker.js"),
        os.path.join("static", "scripts", "store+json2.min.js"),
        os.path.join("templates", "error.html"),
        os.path.join("templates", "error-complete.html"),
        os.path.join("templates", "launch.html"),
        os.path.join("templates", "questionnaire.html"),
        os.path.join("templates", "exit_recruiter.html"),
        os.path.join("templates", "exit_recruiter_mturk.html"),
        os.path.join("templates", "waiting.html"),
        os.path.join("templates", "login.html"),
        os.path.join("templates", "dashboard_lifecycle.html"),
        os.path.join("templates", "dashboard_database.html"),
        os.path.join("templates", "dashboard_heroku.html"),
        os.path.join("templates", "dashboard_home.html"),
        os.path.join("templates", "dashboard_monitor.html"),
        os.path.join("templates", "dashboard_mturk.html"),
        os.path.join("static", "robots.txt"),
    ]
    frontend_dirs = [os.path.join("templates", "base")]
    for filename in frontend_files:
        src = os.path.join(dallinger_root, "frontend", filename)
        dst_filepath = os.path.join(dst, filename)
        if not os.path.exists(dst_filepath):
            shutil.copy(src, dst_filepath)
    for filename in frontend_dirs:
        src = os.path.join(dallinger_root, "frontend", filename)
        dst_filepath = os.path.join(dst, filename)
        if not os.path.exists(dst_filepath):
            shutil.copytree(src, dst_filepath)

    # Copy Heroku files
    heroku_files = ["Procfile"]
    for filename in heroku_files:
        src = os.path.join(dallinger_root, "heroku", filename)
        shutil.copy(src, os.path.join(dst, filename))

    # Write out a runtime.txt file based on configuration
    pyversion = config.get("heroku_python_version")
    with open(os.path.join(dst, "runtime.txt"), "w") as file:
        file.write("python-{}".format(pyversion))

    if not config.get("clock_on"):
        # If the clock process has been disabled, overwrite the Procfile:
        src = os.path.join(dallinger_root, "heroku", "Procfile_no_clock")
        shutil.copy(src, os.path.join(dst, "Procfile"))

    ExplicitFileSource(os.getcwd()).selective_copy_to(dst)

    # Overwrite the requirements.txt file with the contents of the constraints.txt file
    (Path(dst) / "constraints.txt").replace((Path(dst) / "requirements.txt"))

    return dst


def setup_experiment(
    log, debug=True, verbose=False, app=None, exp_config=None, local_checks=True
):
    """Checks the experiment's python dependencies, then prepares a temp directory
    with files merged from the custom experiment and Dallinger.

    The resulting directory includes all the files necessary to deploy to
    Heroku.
    """

    # Verify that the Postgres server is running.
    if local_checks:
        try:
            db.check_connection()
        except Exception:
            log("There was a problem connecting to the Postgres database!")
            raise

        # Check that the demo-specific requirements are satisfied.
        try:
            with open("requirements.txt", "r") as f:
                dependencies = [r for r in f.readlines() if r[:3] != "-e "]
        except (OSError, IOError):
            dependencies = []
        pkg_resources.require(dependencies)
    ensure_constraints_file_presence(os.getcwd())
    # Generate a unique id for this experiment.
    from dallinger.experiment import Experiment

    experiment_uid = heroku_app_id = Experiment.make_uuid(app)
    log("Experiment UID: {}".format(experiment_uid))

    # Load and update the config
    config = get_config()
    if not config.ready:
        config.load()  #
    if exp_config:
        config.extend(exp_config)

    # If the user provided an app name, store it. We'll use it as the basis for
    # the Heroku app ID. We still have a fair amount of ambiguity around what
    # this value actually represents (it's not used as _only_ the Heroku app ID).
    if app:
        heroku_app_id = str(app)
        log("Using custom Heroku ID root: {}".format(heroku_app_id))

    config.extend(
        {
            "id": six.text_type(experiment_uid),
            "heroku_app_id_root": six.text_type(heroku_app_id),
        }
    )

    if not config.get("dashboard_password", None):
        config.set("dashboard_password", fake.password(length=20, special_chars=False))

    temp_dir = assemble_experiment_temp_dir(config)
    log("Deployment temp directory: {}".format(temp_dir), chevrons=False)

    # Zip up the temporary directory and place it in the cwd.
    if not debug:
        log("Freezing the experiment package...")
        shutil.make_archive(
            os.path.join(os.getcwd(), "snapshots", heroku_app_id + "-code"),
            "zip",
            temp_dir,
        )

    return (heroku_app_id, temp_dir)


def ensure_constraints_file_presence(directory: str):
    """Looks into the path represented by the string `directory`.
    Does nothing if a `constraints.txt` file exists there.
    Otherwise it creates the constraints.txt file based on the
    contents of the `requirements.txt` file.
    If the `requirements.txt` does not exist one is created with
    `dallinger` as its only dependency.
    """
    constraints_path = Path(directory) / "constraints.txt"
    requirements_path = Path(directory) / "requirements.txt"
    if constraints_path.exists():
        return
    if not requirements_path.exists():
        requirements_path.write_text("dallinger\n")
    os.environ[
        "CUSTOM_COMPILE_COMMAND"
    ] = "rm constraints.txt\n#\n# and re-run dallinger in this experiment directory."
    prev_cwd = os.getcwd()
    try:
        os.chdir(directory)
        os.system("pip-compile requirements.txt -o constraints.txt")
    finally:
        os.chdir(prev_cwd)
    # Make the path the experiment requirements.txt file relative
    constraints_contents = constraints_path.read_text()
    constraints_contents_amended = re.sub(
        "via -r .*requirements.txt", "via -r requirements.txt", constraints_contents
    )
    constraints_path.write_text(constraints_contents_amended)


INITIAL_DELAY = 1
BACKOFF_FACTOR = 2
MAX_ATTEMPTS = 6


def _handle_launch_data(url, error, delay=INITIAL_DELAY, attempts=MAX_ATTEMPTS):
    for remaining_attempt in sorted(range(attempts), reverse=True):  # [3, 2, 1, 0]
        time.sleep(delay)
        launch_request = requests.post(url)
        try:
            launch_data = launch_request.json()
        except ValueError:
            error(
                "Error parsing response from {}, "
                "check web dyno logs for details: {}".format(url, launch_request.text)
            )
            raise

        # Early return if successful
        if launch_request.ok:
            return launch_data

        error(
            "Error accessing {} ({}):\n{}".format(
                url, launch_request.status_code, launch_request.text
            )
        )

        if remaining_attempt:
            delay = delay * BACKOFF_FACTOR
            next_attempt_count = attempts - (remaining_attempt - 1)
            error(
                "Experiment launch failed. Trying again "
                "(attempt {} of {}) in {} seconds ...".format(
                    next_attempt_count, attempts, delay
                )
            )

    error("Experiment launch failed, check web dyno logs for details.")
    if launch_data.get("message"):
        error(launch_data["message"])
    launch_request.raise_for_status()


def deploy_sandbox_shared_setup(
    log, verbose=True, app=None, exp_config=None, prelaunch_actions=None
):
    """Set up Git, push to Heroku, and launch the app."""
    if verbose:
        out = None
    else:
        out = open(os.devnull, "w")

    config = get_config()
    if not config.ready:
        config.load()
    heroku.sanity_check(config)
    (heroku_app_id, tmp) = setup_experiment(
        log, debug=False, app=app, exp_config=exp_config
    )

    # Register the experiment using all configured registration services.
    if config.get("mode") == "live":
        log("Registering the experiment on configured services...")
        registration.register(heroku_app_id, snapshot=None)

    # Log in to Heroku if we aren't already.
    log("Making sure that you are logged in to Heroku.")
    heroku.log_in()
    config.set("heroku_auth_token", heroku.auth_token())
    log("", chevrons=False)

    # Change to temporary directory.
    cwd = os.getcwd()
    os.chdir(tmp)

    # Commit Heroku-specific files to tmp folder's git repo.
    git = GitClient(output=out)
    git.init()
    git.add("--all")
    git.commit('"Experiment {}"'.format(heroku_app_id))

    # Initialize the app on Heroku.
    log("Initializing app on Heroku...")
    team = config.get("heroku_team", None)
    heroku_app = HerokuApp(dallinger_uid=heroku_app_id, output=out, team=team)
    heroku_app.bootstrap()
    heroku_app.buildpack("https://github.com/stomita/heroku-buildpack-phantomjs")

    # Set up add-ons and AWS environment variables.
    database_size = config.get("database_size")
    redis_size = config.get("redis_size")
    addons = [
        "heroku-postgresql:{}".format(quote(database_size)),
        "heroku-redis:{}".format(quote(redis_size)),
        "papertrail",
    ]
    if config.get("sentry"):
        addons.append("sentry")

    for name in addons:
        heroku_app.addon(name)

    heroku_config = {
        "aws_access_key_id": config["aws_access_key_id"],
        "aws_secret_access_key": config["aws_secret_access_key"],
        "aws_region": config["aws_region"],
        "auto_recruit": config["auto_recruit"],
        "smtp_username": config["smtp_username"],
        "smtp_password": config["smtp_password"],
        "whimsical": config["whimsical"],
        "FLASK_SECRET_KEY": codecs.encode(os.urandom(16), "hex").decode("ascii"),
    }

    # Set up the preferred class as an environment variable, if one is set
    # This is needed before the config is parsed, but we also store it in the
    # config to make things easier for recording into bundles.
    preferred_class = config.get("EXPERIMENT_CLASS_NAME", None)
    if preferred_class:
        heroku_config["EXPERIMENT_CLASS_NAME"] = preferred_class

    heroku_app.set_multiple(**heroku_config)

    # Wait for Redis database to be ready.
    log("Waiting for Redis (this can take a couple minutes)...", nl=False)
    ready = False
    while not ready:
        try:
            r = connect_to_redis(url=heroku_app.redis_url)
            r.set("foo", "bar")
            ready = True
            log("\n✓ connected at {}".format(heroku_app.redis_url), chevrons=False)
        except (ValueError, redis.exceptions.ConnectionError):
            time.sleep(2)
            log(".", chevrons=False, nl=False)

    log("Saving the URL of the postgres database...")
    config.extend({"database_url": heroku_app.db_url})
    config.write()
    git.add("config.txt")
    git.commit("Save URL for database")

    log("Generating dashboard links...")
    heroku_addons = heroku_app.addon_parameters()
    heroku_addons = json.dumps(heroku_addons)
    if six.PY2:
        heroku_addons = heroku_addons.decode("utf-8")
    config.extend({"infrastructure_debug_details": heroku_addons})
    config.write()
    git.add("config.txt")
    git.commit("Save URLs for heroku addon management")

    # Launch the Heroku app.
    log("Pushing code to Heroku...")
    git.push(remote="heroku", branch="HEAD:master")

    log("Scaling up the dynos...")
    default_size = config.get("dyno_type")
    for process in ["web", "worker"]:
        size = config.get("dyno_type_" + process, default_size)
        qty = config.get("num_dynos_" + process)
        heroku_app.scale_up_dyno(process, qty, size)
    if config.get("clock_on"):
        heroku_app.scale_up_dyno("clock", 1, size)

    if prelaunch_actions is not None:
        for task in prelaunch_actions:
            task(heroku_app, config)

    # Launch the experiment.
    log("Launching the experiment on the remote server and starting recruitment...")
    launch_url = "{}/launch".format(heroku_app.url)
    log("Calling {}".format(launch_url), chevrons=False)
    launch_data = _handle_launch_data(launch_url, error=log)
    result = {
        "app_name": heroku_app.name,
        "app_home": heroku_app.url,
        "dashboard_url": "{}/dashboard/".format(heroku_app.url),
        "recruitment_msg": launch_data.get("recruitment_msg", None),
    }

    log("Experiment details:")
    log("App home: {}".format(result["app_home"]), chevrons=False)
    log("Dashboard URL: {}".format(result["dashboard_url"]), chevrons=False)
    log("Dashboard user: {}".format(config.get("dashboard_user")), chevrons=False)
    log(
        "Dashboard password: {}".format(config.get("dashboard_password")),
        chevrons=False,
    )

    log("Recruiter info:")
    log(result["recruitment_msg"], chevrons=False)

    # Return to the branch whence we came.
    os.chdir(cwd)

    log(
        "Completed Heroku deployment of experiment ID {} using app ID {}.".format(
            config.get("id"), heroku_app_id
        )
    )
    return result


class HerokuLocalDeployment(object):

    exp_id = None
    tmp_dir = None
    dispatch = {}  # Subclass may provide handlers for Heroku process output
    environ = None
    bot = False
    DEPLOY_NAME = "Heroku"
    WRAPPER_CLASS = HerokuLocalWrapper
    DO_INIT_DB = True

    def configure(self):
        self.exp_config.update({"mode": "debug", "loglevel": 0})

    def setup(self):
        self.exp_id, self.tmp_dir = setup_experiment(
            self.out.log, exp_config=self.exp_config
        )

    def update_dir(self):
        # FIXME: this call is used for implicit communication between classes in this file
        # and service wrappers (HerokuLocalWrapper, DockerComposeWrapper).
        # This communication should be made explicit, passing this path around instead of
        # changing a global state.
        os.chdir(self.tmp_dir)
        # Update the logfile to the new directory
        config = get_config()
        logfile = config.get("logfile")
        if logfile and logfile != "-":
            logfile = os.path.join(self.original_dir, logfile)
            config.extend({"logfile": logfile})
        config.write()

    def run(self):
        """Set up the environment, get a wrapper instance, and pass
        it to the concrete class's execute() method.
        """
        self.configure()
        self.setup()
        self.update_dir()
        if self.DO_INIT_DB:
            db.init_db(drop_all=True)
        config = get_config()
        environ = None
        if self.environ:
            environ = os.environ.copy()
            environ.update(self.environ)
        self.out.log(f"Starting up the {self.DEPLOY_NAME} Local server...")
        with self.WRAPPER_CLASS(
            config,
            self.out,
            self.original_dir,
            self.tmp_dir,
            verbose=self.verbose,
            env=environ,
            needs_chrome=self.bot,
        ) as wrapper:
            try:
                self.execute(wrapper)
            except KeyboardInterrupt:
                pass
            finally:
                os.chdir(self.original_dir)
                self.cleanup()

    def notify(self, message):
        """Callback function which checks lines of output, tries to match
        against regex defined in subclass's "dispatch" dict, and passes through
        to a handler on match.
        """
        for regex, handler in self.dispatch.items():
            match = re.search(regex, message)
            if match:
                handler = getattr(self, handler)
                return handler(match)

    def execute(self, heroku):
        raise NotImplementedError()


class DebugDeployment(HerokuLocalDeployment):

    dispatch = {
        r"[^\"]{} (.*)$".format(recruiters.NEW_RECRUIT_LOG_PREFIX): "new_recruit",
        r"{}".format(recruiters.CLOSE_RECRUITMENT_LOG_PREFIX): "recruitment_closed",
    }

    def __init__(self, output, verbose, bot, proxy_port, exp_config, no_browsers=False):
        self.out = output
        self.verbose = verbose
        self.bot = bot
        self.exp_config = exp_config or {}
        self.proxy_port = proxy_port
        self.original_dir = os.getcwd()
        self.complete = False
        self.status_thread = None
        self.no_browsers = no_browsers
        self.environ = {
            "FLASK_SECRET_KEY": codecs.encode(os.urandom(16), "hex").decode("ascii"),
        }

    def configure(self):
        super(DebugDeployment, self).configure()
        if self.bot:
            self.exp_config["recruiter"] = "bots"

    def execute(self, heroku):
        base_url = get_base_url()
        self.out.log("Server is running on {}. Press Ctrl+C to exit.".format(base_url))
        self.out.log("Launching the experiment...")
        try:
            result = _handle_launch_data(
                "{}/launch".format(base_url), error=self.out.error, attempts=1
            )
        except Exception:
            # Show output from server
            self.dispatch[r"POST /launch"] = "launch_request_complete"
            heroku.monitor(listener=self.notify)
        else:
            if result["status"] == "success":
                self.out.log(result["recruitment_msg"])
                dashboard_url = "{}/dashboard/".format(get_base_url())
                self.display_dashboard_access_details(dashboard_url)
                if not self.no_browsers:
                    self.open_dashboard(dashboard_url)
                self.heroku = heroku
                self.out.log(
                    "Monitoring the Heroku Local server for recruitment or completion..."
                )
                heroku.monitor(listener=self.notify)

    def launch_request_complete(self, match):
        return HerokuLocalWrapper.MONITOR_STOP

    def cleanup(self):
        self.out.log("Completed debugging of experiment with id " + self.exp_id)
        self.complete = True

    def new_recruit(self, match):
        """Dispatched to by notify(). If a recruitment request has been issued,
        open a browser window for the a new participant (in this case the
        person doing local debugging).
        """
        self.out.log("new recruitment request!")
        if self.no_browsers:
            self.out.log(recruiters.NEW_RECRUIT_LOG_PREFIX + ": " + match.group(1))
            return
        url = match.group(1)
        if self.proxy_port is not None:
            self.out.log("Using proxy port {}".format(self.proxy_port))
            url = url.replace(str(get_config().get("base_port")), self.proxy_port)
        open_browser(url)

    def display_dashboard_access_details(self, url):
        config = get_config()
        self.out.log("Experiment dashboard: {}".format(url))
        self.out.log(
            "Dashboard user: {} password: {}".format(
                config.get("dashboard_user"),
                config.get("dashboard_password"),
            )
        )

    def open_dashboard(self, url):
        config = get_config()
        self.out.log("Opening dashboard")
        parsed = list(urlparse(url))
        parsed[1] = "{}:{}@{}".format(
            config.get("dashboard_user"),
            config.get("dashboard_password"),
            parsed[1],
        )
        open_browser(urlunparse(parsed))

    def recruitment_closed(self, match):
        """Recruitment is closed.

        Start a thread to check the experiment summary.
        """
        if self.no_browsers:
            self.out.log(recruiters.CLOSE_RECRUITMENT_LOG_PREFIX)
        if self.status_thread is None:
            self.status_thread = threading.Thread(target=self.check_status)
            self.status_thread.start()

    def check_status(self):
        """Check the output of the summary route until
        the experiment is complete, then we can stop monitoring Heroku
        subprocess output.
        """
        self.out.log("Recruitment is complete. Waiting for experiment completion...")
        base_url = get_base_url()
        status_url = base_url + "/summary"
        while not self.complete:
            time.sleep(10)
            try:
                resp = requests.get(status_url)
                exp_data = resp.json()
            except (ValueError, requests.exceptions.RequestException):
                self.out.error("Error fetching experiment status.")
            else:
                self.out.log("Experiment summary: {}".format(exp_data))
                if exp_data.get("completed", False):
                    self.out.log("Experiment completed, all nodes filled.")
                    self.complete = True
                    self.heroku.stop()

    def notify(self, message):
        """Monitor output from heroku process.

        This overrides the base class's `notify`
        to make sure that we stop if the status-monitoring thread
        has determined that the experiment is complete.
        """
        if self.complete:
            return HerokuLocalWrapper.MONITOR_STOP
        return super(DebugDeployment, self).notify(message)


class LoaderDeployment(HerokuLocalDeployment):
    dispatch = {"Replay ready: (.*)$": "start_replay"}

    def __init__(self, app_id, output, verbose, exp_config):
        self.app_id = app_id
        self.out = output
        self.verbose = verbose
        self.exp_config = exp_config or {}
        self.original_dir = os.getcwd()
        self.zip_path = None

    def configure(self):
        self.exp_config.update({"mode": "debug", "loglevel": 0})

        self.zip_path = data.find_experiment_export(self.app_id)
        if self.zip_path is None:
            msg = 'Dataset export for app id "{}" could not be found.'
            raise IOError(msg.format(self.app_id))

    def setup(self):
        self.exp_id, self.tmp_dir = setup_experiment(
            self.out.log, app=self.app_id, exp_config=self.exp_config
        )

    def execute(self, heroku):
        """Start the server, load the zip file into the database, then loop
        until terminated with <control>-c.
        """
        db.init_db(drop_all=True)
        self.out.log(
            "Ingesting dataset from {}...".format(os.path.basename(self.zip_path))
        )
        data.ingest_zip(self.zip_path)
        base_url = get_base_url()
        self.out.log("Server is running on {}. Press Ctrl+C to exit.".format(base_url))

        if self.exp_config.get("replay"):
            self.out.log("Launching the experiment...")
            time.sleep(4)
            _handle_launch_data("{}/launch".format(base_url), error=self.out.error)
            heroku.monitor(listener=self.notify)

        # Just run until interrupted:
        while self.keep_running():
            time.sleep(1)

    def start_replay(self, match):
        """Dispatched to by notify(). If a recruitment request has been issued,
        open a browser window for the a new participant (in this case the
        person doing local debugging).
        """
        self.out.log("replay ready!")
        url = match.group(1)
        open_browser(url)

    def cleanup(self):
        self.out.log("Terminating dataset load for experiment {}".format(self.exp_id))

    def keep_running(self):
        # This is a separate method so that it can be replaced in tests
        return True
