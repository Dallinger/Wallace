#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import unicode_literals
import mock
import os
import pexpect
import pytest
import re
import shutil
import six
import sys
import tempfile
import uuid
from pytest import raises
from six.moves import configparser
from pathlib import Path

from dallinger.config import get_config
from dallinger import recruiters


def found_in(name, path):
    return os.path.exists(os.path.join(path, name))


@pytest.fixture
def output():
    from dallinger.command_line import Output

    return Output(log=mock.Mock(), error=mock.Mock(), blather=mock.Mock())


@pytest.fixture
def browser():
    with mock.patch("dallinger.deployment.open_browser") as open_browser:
        yield open_browser


@pytest.fixture
def faster(tempdir, active_config):
    with mock.patch.multiple(
        "dallinger.deployment", time=mock.DEFAULT, setup_experiment=mock.DEFAULT
    ) as mocks:
        mocks["setup_experiment"].return_value = ("fake-uid", tempdir)
        # setup_experiment normally sets the dashboard credentials if unset
        active_config.extend(
            {
                "dashboard_user": six.text_type("admin"),
                "dashboard_password": six.text_type("DUMBPASSWORD"),
            }
        )
        yield mocks


@pytest.fixture
def launch():
    with mock.patch("dallinger.deployment._handle_launch_data") as hld:
        hld.return_value = {"recruitment_msg": "fake\nrecruitment\nlist"}
        yield hld


@pytest.fixture
def fake_git():
    with mock.patch("dallinger.deployment.GitClient") as git:
        yield git


@pytest.fixture
def fake_redis():
    mock_connection = mock.Mock(name="fake redis connection")
    with mock.patch("dallinger.deployment.connect_to_redis") as connect:
        connect.return_value = mock_connection
        yield mock_connection


@pytest.fixture
def herokuapp():
    # Patch addon since we're using a free app which doesn't support them:
    from dallinger.heroku.tools import HerokuApp

    instance = HerokuApp("fake-uid", output=None, team=None)
    instance.addon = mock.Mock()
    with mock.patch("dallinger.deployment.HerokuApp") as mock_app_class:
        mock_app_class.return_value = instance
        yield instance
        instance.destroy()


@pytest.fixture
def heroku_mock():
    # Patch addon since we're using a free app which doesn't support them:
    from dallinger.heroku.tools import HerokuApp

    instance = mock.Mock(spec=HerokuApp)
    instance.redis_url = "\n"
    instance.name = "dlgr-fake-uid"
    instance.url = "fake-web-url"
    instance.db_url = "fake-db-url"
    instance.addon_parameters.return_value = {}
    with mock.patch("dallinger.deployment.heroku") as heroku_module:
        heroku_module.auth_token.return_value = "fake token"
        with mock.patch("dallinger.deployment.HerokuApp") as mock_app_class:
            mock_app_class.return_value = instance
            yield instance


@pytest.mark.usefixtures("in_tempdir")
class TestExperimentFilesSource(object):
    @pytest.fixture
    def git(self):
        from dallinger.utils import GitClient

        return GitClient()

    @pytest.fixture
    def subject(self):
        from dallinger.deployment import ExperimentFileSource

        return ExperimentFileSource

    def test_lists_files_valid_for_copying(self, subject):
        legit_file = "./some/subdir/John Doe's file.txt"
        os.makedirs(os.path.dirname(legit_file))
        with open(legit_file, "w") as f:
            f.write("12345")

        source = subject()

        assert legit_file in source.files

    def test_excludes_files_that_should_not_be_copied(self, subject):
        with open("illegit.db", "w") as f:
            f.write("12345")

        source = subject()

        assert len(source.files) == 0

    def test_excludes_otherwise_valid_files_if_in_gitignore_simple(self, subject, git):
        legit_file = "./some/subdir/legit.txt"
        os.makedirs(os.path.dirname(legit_file))
        with open(legit_file, "w") as f:
            f.write("12345")
        with open(".gitignore", "w") as f:
            f.write("*.txt")
        git.init()

        source = subject()

        assert source.files == {"./.gitignore"}

    def test_excludes_otherwise_valid_files_if_in_gitignore_complex(self, subject, git):
        legit_file = "./some/subdir/legit.txt"
        os.makedirs(os.path.dirname(legit_file))
        with open(legit_file, "w") as f:
            f.write("12345")
        with open(".gitignore", "w") as f:
            f.write("**/subdir/*")
        git.init()

        source = subject()

        assert source.files == {"./.gitignore"}

    def test_normalizes_unicode_for_merging_git_inclusions(self, subject, git):
        legit_file = "".join(
            [".", "/", "a", "̊", " ", "f", "i", "l", "e", ".", "t", "x", "t"]
        )
        with open(legit_file, "w") as f:
            f.write("12345")
        git.init()

        source = subject()

        assert source.files == {legit_file}

    def test_size_includes_files_that_would_be_copied(self, subject):
        with open("legit.txt", "w") as f:
            f.write("12345")

        source = subject()

        assert source.size == 5

    def test_size_excludes_files_that_would_not_be_copied(self, subject):
        with open("illegit.db", "w") as f:
            f.write("12345")

        source = subject()

        assert source.size == 0

    def test_size_excludes_directories_that_would_not_be_copied(self, subject):
        os.mkdir("snapshots")
        with open("snapshots/legit.txt", "w") as f:
            f.write("12345")

        source = subject()

        assert source.size == 0

    def test_size_excludes_bad_files_when_in_subdirectories(self, subject):
        os.mkdir("legit_dir")
        with open("legit_dir/illegit.db", "w") as f:
            f.write("12345")

        source = subject()

        assert source.size == 0

    def test_copy_to_copies_to_same_subdirectories(self, subject):
        legit_file = "./some/subdir/John Doe's file.txt"
        os.makedirs(os.path.dirname(legit_file))
        with open(legit_file, "w") as f:
            f.write("12345")
        destination = tempfile.mkdtemp()
        source = subject()

        source.selective_copy_to(destination)

        assert os.path.isfile(
            os.path.join(destination, "some/subdir/John Doe's file.txt")
        )

    def test_copy_to_copies_with_explicit_root(self, subject):
        legit_file = "./some/subdir/legit.txt"
        os.makedirs(os.path.dirname(legit_file))
        with open(legit_file, "w") as f:
            f.write("12345")
        destination = tempfile.mkdtemp()
        source = subject(os.getcwd())

        source.selective_copy_to(destination)

        assert os.path.isfile(os.path.join(destination, "some/subdir/legit.txt"))

    def test_copy_to_copies_nonascii_filenames(self, subject):
        legit_file = "".join(["a", "̊", " ", "f", "i", "l", "e"])
        with open(legit_file, "w") as f:
            f.write("12345")
        destination = tempfile.mkdtemp()
        source = subject()

        source.selective_copy_to(destination)

        assert os.path.isfile(os.path.join(destination, legit_file))

    def test_copy_to_copies_nonascii_filenames2(self, subject):
        legit_file = "".join(["å", " ", "f", "i", "l", "e"])
        with open(legit_file, "w") as f:
            f.write("12345")
        destination = tempfile.mkdtemp()
        source = subject()

        source.selective_copy_to(destination)

        assert os.path.isfile(os.path.join(destination, legit_file))


@pytest.mark.usefixtures("bartlett_dir", "active_config", "reset_sys_modules")
class TestSetupExperiment(object):
    @pytest.fixture
    def setup_experiment(self, env):
        from dallinger.deployment import setup_experiment as subject

        return subject

    def test_generates_exp_and_app_uid_if_none_provided(self, setup_experiment):
        exp_id, dst = setup_experiment(log=mock.Mock())

        assert isinstance(uuid.UUID(exp_id, version=4), uuid.UUID)

    def test_generated_uid_saved_to_config(self, active_config, setup_experiment):
        exp_id, dst = setup_experiment(log=mock.Mock())

        assert active_config.get("id") == exp_id

    def test_uses_provided_app_uid(self, setup_experiment):
        exp_id, dst = setup_experiment(log=mock.Mock(), app="my-custom-app-id")

        assert exp_id == "my-custom-app-id"

    def test_saves_provided_app_uid_to_config(self, active_config, setup_experiment):
        exp_id, dst = setup_experiment(log=mock.Mock(), app="my-custom-app-id")

        assert "my-custom-app-id" == active_config.get("heroku_app_id_root")

    def test_still_saves_uuid_in_addition_to_custom_app_id(
        self, active_config, setup_experiment
    ):
        exp_id, dst = setup_experiment(log=mock.Mock(), app="my-custom-app-id")

        assert isinstance(uuid.UUID(active_config.get("id"), version=4), uuid.UUID)

    def test_dashboard_credentials_saved_to_config(
        self, active_config, setup_experiment
    ):
        exp_id, dst = setup_experiment(log=mock.Mock())

        assert active_config.get("dashboard_user") == six.text_type("admin")
        assert active_config.get("dashboard_password") == mock.ANY

    def test_setup_creates_new_experiment(self, setup_experiment):
        # Baseline
        exp_dir = os.getcwd()
        assert found_in("experiment.py", exp_dir)
        assert not found_in("experiment_id.txt", exp_dir)
        assert not found_in("Procfile", exp_dir)
        assert not found_in("runtime.txt", exp_dir)

        exp_id, dst = setup_experiment(log=mock.Mock())

        # dst should be a temp dir with a cloned experiment for deployment
        assert exp_dir != dst
        assert "/tmp" in dst

        assert found_in("experiment_id.txt", dst)
        assert found_in("experiment.py", dst)
        assert found_in("models.py", dst)
        assert found_in("Procfile", dst)
        assert found_in("runtime.txt", dst)

        assert found_in(os.path.join("static", "css", "dallinger.css"), dst)
        assert found_in(os.path.join("static", "scripts", "dallinger2.js"), dst)
        assert found_in(
            os.path.join("static", "scripts", "reconnecting-websocket.js"), dst
        )
        assert found_in(os.path.join("static", "scripts", "reqwest.min.js"), dst)
        assert found_in(os.path.join("static", "scripts", "spin.min.js"), dst)
        assert found_in(os.path.join("static", "scripts", "store+json2.min.js"), dst)
        assert found_in(os.path.join("static", "robots.txt"), dst)
        assert found_in(os.path.join("templates", "error.html"), dst)
        assert found_in(os.path.join("templates", "error-complete.html"), dst)
        assert found_in(os.path.join("templates", "exit_recruiter.html"), dst)
        assert found_in(os.path.join("templates", "exit_recruiter_mturk.html"), dst)
        assert found_in(os.path.join("templates", "launch.html"), dst)

    def test_setup_uses_specified_python_version(self, active_config, setup_experiment):
        active_config.extend({"heroku_python_version": "3.8.7"})

        exp_id, dst = setup_experiment(log=mock.Mock())

        with open(os.path.join(dst, "runtime.txt"), "r") as file:
            version = file.read()

        assert version == "python-3.8.7"

    def test_setup_procfile_no_clock(self, setup_experiment):
        config = get_config()
        config.set("clock_on", False)
        assert config.get("clock_on") is False
        exp_dir = os.getcwd()
        assert not found_in("Procfile", exp_dir)

        exp_id, dst = setup_experiment(log=mock.Mock())

        assert found_in("Procfile", dst)
        with open(os.path.join(dst, "Procfile")) as proc:
            assert "clock: dallinger_heroku_clock" not in [p.strip() for p in proc]

    def test_setup_procfile_with_clock(self, setup_experiment):
        config = get_config()
        config.set("clock_on", True)
        assert config.get("clock_on") is True
        exp_dir = os.getcwd()
        assert not found_in("Procfile", exp_dir)

        exp_id, dst = setup_experiment(log=mock.Mock())

        assert found_in("Procfile", dst)
        with open(os.path.join(dst, "Procfile")) as proc:
            assert "clock: dallinger_heroku_clock" in [p.strip() for p in proc]

    def test_setup_with_custom_dict_config(self, setup_experiment):
        config = get_config()
        assert config.get("num_dynos_web") == 1

        exp_id, dst = setup_experiment(log=mock.Mock(), exp_config={"num_dynos_web": 2})
        # Config is updated
        assert config.get("num_dynos_web") == 2

        # Code snapshot is saved
        os.path.exists(os.path.join("snapshots", exp_id + "-code.zip"))

        # There should be a modified configuration in the temp dir
        deploy_config = configparser.ConfigParser()
        deploy_config.read(os.path.join(dst, "config.txt"))
        assert int(deploy_config.get("Parameters", "num_dynos_web")) == 2

    def test_setup_excludes_sensitive_config(self, setup_experiment):
        config = get_config()
        # Auto detected as sensitive
        config.register("a_password", six.text_type)
        # Manually registered as sensitive
        config.register("something_sensitive", six.text_type, sensitive=True)
        # Not sensitive at all
        config.register("something_normal", six.text_type)

        config.extend(
            {
                "a_password": "secret thing",
                "something_sensitive": "hide this",
                "something_normal": "show this",
            }
        )

        exp_id, dst = setup_experiment(log=mock.Mock())

        # The temp dir should have a config with the sensitive variables missing
        deploy_config = configparser.ConfigParser()
        deploy_config.read(os.path.join(dst, "config.txt"))
        assert deploy_config.get("Parameters", "something_normal") == "show this"
        with raises(configparser.NoOptionError):
            deploy_config.get("Parameters", "a_password")
        with raises(configparser.NoOptionError):
            deploy_config.get("Parameters", "something_sensitive")

    def test_reraises_db_connection_error(self, setup_experiment):
        from psycopg2 import OperationalError

        with mock.patch("dallinger.deployment.db.check_connection") as checker:
            checker.side_effect = OperationalError("Boom!")
            with pytest.raises(Exception) as ex_info:
                setup_experiment(log=mock.Mock())
                assert ex_info.match("Boom!")


@pytest.mark.usefixtures("experiment_dir", "active_config", "reset_sys_modules")
class TestSetupExperimentAdditional(object):
    @pytest.fixture
    def setup_experiment(self):
        from dallinger.deployment import setup_experiment as subject

        return subject

    def test_additional_files_can_be_included_by_module_function(
        self, setup_experiment
    ):
        # Baseline
        exp_dir = os.getcwd()
        assert found_in("dallinger_experiment.py", exp_dir)
        assert not found_in("experiment_id.txt", exp_dir)
        assert not found_in("Procfile", exp_dir)
        assert not found_in("runtime.txt", exp_dir)

        exp_id, dst = setup_experiment(log=mock.Mock())

        # dst should be a temp dir with a cloned experiment for deployment
        assert exp_dir != dst
        assert "/tmp" in dst

        assert found_in("experiment_id.txt", dst)
        assert found_in("dallinger_experiment.py", dst)

        # Files specified individually are copied
        assert found_in(os.path.join("static", "expfile.txt"), dst)
        # As are ones specified as part of a directory
        assert found_in(os.path.join("static", "copied_templates", "ad.html"), dst)

    def test_warning_if_multiple_experiments_found(
        self, active_config, setup_experiment
    ):
        with mock.patch("warnings.warn") as warn:
            _, _ = setup_experiment(log=mock.Mock())

        assert len(warn.mock_calls) == 1
        e = warn.mock_calls[0][1][0]
        assert "EXPERIMENT_CLASS_NAME" in str(e)
        assert (
            "Picking TestExperiment from ['TestExperiment', 'ZSubclassThatSortsLower']"
            in str(e)
        )

        # No warning raised if we set the variable
        try:
            os.environ["EXPERIMENT_CLASS_NAME"] = "ZSubclassThatSortsLower"
            with mock.patch("warnings.warn") as warn:
                exp_id, dst = setup_experiment(log=mock.Mock())
            assert len(warn.mock_calls) == 0
        finally:
            del os.environ["EXPERIMENT_CLASS_NAME"]

    def test_additional_files_can_be_included_by_exp_classmethod(
        self, active_config, setup_experiment
    ):
        # Baseline
        exp_dir = os.getcwd()
        assert found_in("dallinger_experiment.py", exp_dir)
        assert not found_in("experiment_id.txt", exp_dir)
        assert not found_in("Procfile", exp_dir)
        assert not found_in("runtime.txt", exp_dir)

        try:
            os.environ["EXPERIMENT_CLASS_NAME"] = "ZSubclassThatSortsLower"
            exp_id, dst = setup_experiment(log=mock.Mock())
        finally:
            del os.environ["EXPERIMENT_CLASS_NAME"]

        # dst should be a temp dir with a cloned experiment for deployment
        assert exp_dir != dst
        assert "/tmp" in dst

        assert found_in("experiment_id.txt", dst)
        assert found_in("dallinger_experiment.py", dst)

        # Files specified individually are copied
        assert found_in(os.path.join("static", "different.txt"), dst)
        # As are ones specified as part of a directory
        assert found_in(os.path.join("static", "different", "ad.html"), dst)


@pytest.mark.usefixtures("active_config", "launch", "fake_git", "fake_redis", "faster")
class TestDeploySandboxSharedSetupNoExternalCalls(object):
    @pytest.fixture
    def dsss(self):
        from dallinger.deployment import deploy_sandbox_shared_setup

        return deploy_sandbox_shared_setup

    def test_result(self, dsss, heroku_mock):
        log = mock.Mock()
        result = dsss(log=log)
        assert result == {
            "app_home": "fake-web-url",
            "app_name": "dlgr-fake-uid",
            "dashboard_url": "fake-web-url/dashboard/",
            "recruitment_msg": "fake\nrecruitment\nlist",
        }

    def test_bootstraps_heroku(self, dsss, heroku_mock):
        dsss(log=mock.Mock())
        heroku_mock.bootstrap.assert_called_once()

    def test_installs_phantomjs(self, dsss, heroku_mock):
        dsss(log=mock.Mock())
        heroku_mock.buildpack.assert_called_once_with(
            "https://github.com/stomita/heroku-buildpack-phantomjs"
        )

    def test_installs_addons(self, dsss, heroku_mock):
        dsss(log=mock.Mock())
        heroku_mock.addon.assert_has_calls(
            [
                mock.call("heroku-postgresql:standard-0"),
                mock.call("heroku-redis:premium-0"),
                mock.call("papertrail"),
                mock.call("sentry"),
            ]
        )

    def test_sets_app_properties(self, dsss, heroku_mock):
        dsss(log=mock.Mock())
        heroku_mock.set_multiple.assert_called_once_with(
            auto_recruit=True,
            aws_access_key_id="fake aws key",
            aws_region="us-east-1",
            aws_secret_access_key="fake aws secret",
            FLASK_SECRET_KEY=mock.ANY,  # password is random
            smtp_password="fake email password",
            smtp_username="fake email username",
            whimsical=True,
        )

    def test_adds_db_url_to_config(self, dsss, heroku_mock, active_config):
        dsss(log=mock.Mock())
        assert active_config.get("database_url") == heroku_mock.db_url

    def test_verifies_working_redis(self, dsss, heroku_mock, fake_redis):
        dsss(log=mock.Mock())
        fake_redis.set.assert_called_once_with("foo", "bar")

    def test_scales_dynos(self, dsss, heroku_mock, active_config):
        active_config.set("clock_on", True)
        dsss(log=mock.Mock())
        heroku_mock.scale_up_dyno.assert_has_calls(
            [
                mock.call("web", 1, "free"),
                mock.call("worker", 1, "free"),
                mock.call("clock", 1, "free"),
            ]
        )

    def test_scales_different_dynos(self, dsss, heroku_mock, active_config):
        active_config.set("dyno_type", "ignored")
        active_config.set("dyno_type_web", "tiny")
        active_config.set("dyno_type_worker", "massive")
        dsss(log=mock.Mock())
        heroku_mock.scale_up_dyno.assert_has_calls(
            [mock.call("web", 1, "tiny"), mock.call("worker", 1, "massive")]
        )

    def test_calls_launch(self, dsss, heroku_mock, launch):
        log = mock.Mock()
        dsss(log=log)
        launch.assert_called_once_with("fake-web-url/launch", error=log)

    def test_heroku_sanity_check(self, dsss, heroku_mock, active_config):
        log = mock.Mock()
        dsss(log=log)
        # Get the patched heroku module
        from dallinger.deployment import heroku

        heroku.sanity_check.assert_called_once_with(active_config)

    def test_runs_prelaunch_actions(self, dsss, heroku_mock, active_config):
        log = mock.Mock()
        action = mock.Mock()
        dsss(log=log, prelaunch_actions=[action])

        action.assert_called_once_with(heroku_mock, active_config)


@pytest.mark.usefixtures("check_heroku")
@pytest.mark.usefixtures("bartlett_dir", "active_config", "launch", "herokuapp")
class TestDeploySandboxSharedSetupFullSystem(object):
    @pytest.fixture
    def dsss(self):
        from dallinger.deployment import deploy_sandbox_shared_setup

        return deploy_sandbox_shared_setup

    def test_full_deployment(self, dsss):
        no_clock = {"clock_on": False}  # can't run clock on free dyno
        result = dsss(
            log=mock.Mock(), exp_config=no_clock
        )  # can't run clock on free dyno
        app_name = result.get("app_name")
        assert app_name.startswith("dlgr")


@pytest.mark.usefixtures("bartlett_dir")
@pytest.mark.slow
class Test_handle_launch_data(object):
    @pytest.fixture
    def handler(self):
        from dallinger.deployment import _handle_launch_data

        return _handle_launch_data

    def test_success(self, handler):
        log = mock.Mock()
        with mock.patch("dallinger.deployment.requests.post") as mock_post:
            result = mock.Mock(
                ok=True, json=mock.Mock(return_value={"message": "msg!"})
            )
            mock_post.return_value = result
            assert handler("/some-launch-url", error=log) == {"message": "msg!"}

    def test_failure(self, handler):
        from requests.exceptions import HTTPError

        log = mock.Mock()
        with mock.patch("dallinger.deployment.requests.post") as mock_post:
            mock_post.return_value = mock.Mock(
                ok=False,
                json=mock.Mock(return_value={"message": "msg!"}),
                raise_for_status=mock.Mock(side_effect=HTTPError),
                status_code=500,
                text="Failure",
            )
            with pytest.raises(HTTPError):
                handler("/some-launch-url", error=log, delay=0.05, attempts=3)

        log.assert_has_calls(
            [
                mock.call("Error accessing /some-launch-url (500):\nFailure"),
                mock.call(
                    "Experiment launch failed. Trying again (attempt 2 of 3) in 0.1 seconds ..."
                ),
                mock.call("Error accessing /some-launch-url (500):\nFailure"),
                mock.call(
                    "Experiment launch failed. Trying again (attempt 3 of 3) in 0.2 seconds ..."
                ),
                mock.call("Error accessing /some-launch-url (500):\nFailure"),
                mock.call("Experiment launch failed, check web dyno logs for details."),
                mock.call("msg!"),
            ]
        )

    def test_non_json_response_error(self, handler):
        log = mock.Mock()
        with mock.patch("dallinger.deployment.requests.post") as mock_post:
            mock_post.return_value = mock.Mock(
                json=mock.Mock(side_effect=ValueError), text="Big, unexpected problem."
            )
            with pytest.raises(ValueError):
                handler("/some-launch-url", error=log)

        log.assert_called_once_with(
            "Error parsing response from /some-launch-url, check web dyno logs for details: "
            "Big, unexpected problem."
        )


@pytest.mark.usefixtures("bartlett_dir", "clear_workers", "env")
@pytest.mark.slow
class TestDebugServer(object):
    @pytest.fixture
    def debugger_unpatched(self, output):
        from dallinger.deployment import DebugDeployment

        debugger = DebugDeployment(
            output, verbose=True, bot=False, proxy_port=None, exp_config={}
        )
        yield debugger
        if debugger.status_thread:
            debugger.status_thread.join()

    @pytest.fixture
    def no_browser_debugger(self, output):
        from dallinger.deployment import DebugDeployment

        debugger = DebugDeployment(
            output,
            verbose=True,
            bot=False,
            proxy_port=None,
            exp_config={},
            no_browsers=True,
        )
        yield debugger
        if debugger.status_thread:
            debugger.status_thread.join()

    @pytest.fixture
    def debugger(self, debugger_unpatched):
        from dallinger.heroku.tools import HerokuLocalWrapper

        debugger = debugger_unpatched
        debugger.notify = mock.Mock(return_value=HerokuLocalWrapper.MONITOR_STOP)
        return debugger

    def test_startup(self, debugger):
        debugger.no_browsers = True
        debugger.run()
        "Server is running" in str(debugger.out.log.call_args_list[0])

    def test_raises_if_heroku_wont_start(self, debugger):
        mock_wrapper = mock.Mock(
            __enter__=mock.Mock(side_effect=OSError),
            __exit__=mock.Mock(return_value=False),
        )
        with mock.patch(
            "dallinger.deployment.HerokuLocalDeployment.WRAPPER_CLASS"
        ) as Wrapper:
            Wrapper.return_value = mock_wrapper
            with pytest.raises(OSError):
                debugger.run()

    def test_new_participant(self, debugger_unpatched):
        debugger = debugger_unpatched
        debugger.new_recruit = mock.Mock(return_value=None)
        assert not debugger.new_recruit.called
        debugger.notify(" New participant requested: http://example.com")
        assert debugger.new_recruit.called

    def test_recruitment_closed(self, debugger_unpatched):
        debugger = debugger_unpatched
        debugger.new_recruit = mock.Mock(return_value=None)
        debugger.heroku = mock.Mock()
        response = mock.Mock(json=mock.Mock(return_value={"completed": True}))
        with mock.patch("dallinger.deployment.requests") as mock_requests:
            mock_requests.get.return_value = response
            debugger.notify(recruiters.CLOSE_RECRUITMENT_LOG_PREFIX)
            debugger.status_thread.join()

        debugger.out.log.assert_called_with("Experiment completed, all nodes filled.")
        debugger.heroku.stop.assert_called_once()

    def test_new_recruit(self, debugger_unpatched, browser):
        debugger_unpatched.notify(
            " {} some-fake-url".format(recruiters.NEW_RECRUIT_LOG_PREFIX)
        )

        browser.assert_called_once_with("some-fake-url")

    def test_new_recruit_no_browser(self, no_browser_debugger, browser):
        no_browser_debugger.notify(
            " {} some-fake-url".format(recruiters.NEW_RECRUIT_LOG_PREFIX)
        )
        browser.assert_not_called()

    def test_new_recruit_opens_browser_on_proxy_port(
        self, active_config, debugger_unpatched, browser
    ):
        debugger_unpatched.proxy_port = "2222"
        debugger_unpatched.notify(
            " {} some-fake-url:{}".format(
                recruiters.NEW_RECRUIT_LOG_PREFIX, active_config.get("base_port")
            )
        )
        browser.assert_called_once_with("some-fake-url:2222")

    def test_new_recruit_not_triggered_if_quoted(self, debugger_unpatched, browser):
        debugger_unpatched.notify(
            ' "{}" some-fake-url'.format(recruiters.NEW_RECRUIT_LOG_PREFIX)
        )

        browser.assert_not_called()

    @pytest.mark.usefixtures("check_runbot")
    def test_debug_bots(self, env):
        # Make sure debug server runs to completion with bots
        p = pexpect.spawn(
            "dallinger", ["debug", "--verbose", "--bot"], env=env, encoding="utf-8"
        )
        p.logfile = sys.stdout
        try:
            p.expect_exact("Server is running", timeout=300)
            p.expect_exact("Recruitment is complete", timeout=600)
            p.expect_exact("Experiment completed", timeout=60)
            p.expect_exact("Local Heroku process terminated", timeout=10)
        finally:
            try:
                p.sendcontrol("c")
                p.read()
            except IOError:
                pass

    def test_failure(self, debugger):
        from requests.exceptions import HTTPError

        with mock.patch("dallinger.deployment.HerokuLocalDeployment.WRAPPER_CLASS"):
            with mock.patch("dallinger.deployment.requests.post") as mock_post:
                mock_post.return_value = mock.Mock(
                    ok=False,
                    json=mock.Mock(return_value={"message": "msg!"}),
                    raise_for_status=mock.Mock(side_effect=HTTPError),
                    status_code=500,
                    text="Failure",
                )
                debugger.run()

        # Only one launch attempt should be made in debug mode
        debugger.out.error.assert_has_calls(
            [
                mock.call(
                    "Error accessing http://localhost:5000/launch (500):\nFailure"
                ),
                mock.call("Experiment launch failed, check web dyno logs for details."),
                mock.call("msg!"),
            ]
        )


@pytest.mark.usefixtures("bartlett_dir", "clear_workers", "env")
@pytest.mark.slow
@pytest.mark.docker
class TestDockerServer(object):
    @pytest.mark.skipif(bool(os.environ.get("CI")), reason="Fails when run in the CI")
    def test_docker_debug_with_bots(self, env):
        # Make sure debug server runs to completion with bots
        p = pexpect.spawn(
            "dallinger",
            ["docker", "debug", "--verbose", "--bot", "--no-browsers"],
            env=env,
            encoding="utf-8",
        )
        p.logfile = sys.stdout
        try:
            p.expect_exact("Server is running", timeout=60)
            p.expect_exact("Recruitment is complete", timeout=120)
            p.expect_exact("'status': 'success'", timeout=60)
            p.expect_exact("Experiment completed", timeout=10)
            p.expect_exact("Stopping bartlett1932_web_1", timeout=20)
            p.expect(pexpect.EOF)
        finally:
            try:
                p.sendcontrol("c")
                p.read()
            except IOError:
                pass

    def test_docker_debug_without_bots(self, env):
        sys.path.append(os.getcwd())
        from experiment import Bot

        # Make sure debug server runs to completion without bots
        p = pexpect.spawn(
            "dallinger",
            ["docker", "debug", "--verbose", "--no-browsers"],
            env=env,
            encoding="utf-8",
        )
        p.logfile = sys.stdout
        try:
            p.expect_exact("Server is running", timeout=60)
            p.expect_exact("Initial recruitment list:", timeout=20)
            p.expect("New participant requested.*", 30)
            Bot(re.search("http://[^ \n\r]+", p.after).group()).run_experiment()
            p.expect("New participant requested.*", 30)
            Bot(re.search("http://[^ \n\r]+", p.after).group()).run_experiment()
            p.expect_exact("Recruitment is complete", timeout=120)
            p.expect_exact("'status': 'success'", timeout=60)
            p.expect_exact("Experiment completed", timeout=10)
            p.expect_exact("Stopping bartlett1932_web_1", timeout=20)
            p.expect(pexpect.EOF)
        finally:
            try:
                p.sendcontrol("c")
                p.read()
            except IOError:
                pass


@pytest.mark.usefixtures("bartlett_dir", "clear_workers", "env")
@pytest.mark.slow
class TestLoad(object):

    exp_id = "some_experiment_id"

    @pytest.fixture
    def export(self):
        # Data export created, then removed after test[s]
        from dallinger.data import export

        path = export(self.exp_id, local=True)
        yield path
        os.remove(path)

    @pytest.fixture
    def loader(self, db_session, output, clear_workers):
        from dallinger.deployment import LoaderDeployment
        from dallinger.heroku.tools import HerokuLocalWrapper

        loader = LoaderDeployment(self.exp_id, output, verbose=True, exp_config={})
        loader.notify = mock.Mock(return_value=HerokuLocalWrapper.MONITOR_STOP)

        yield loader

    @pytest.fixture
    def replay_loader(self, db_session, env, output, clear_workers):
        from dallinger.deployment import LoaderDeployment

        loader = LoaderDeployment(
            self.exp_id, output, verbose=True, exp_config={"replay": True}
        )
        loader.keep_running = mock.Mock(return_value=False)

        def launch_and_finish(self):
            from dallinger.heroku.tools import HerokuLocalWrapper

            loader.out.log("Launching replay browser...")
            return HerokuLocalWrapper.MONITOR_STOP

        loader.start_replay = mock.Mock(
            return_value=None, side_effect=launch_and_finish
        )
        yield loader

    def test_load_runs(self, loader, export):
        loader.keep_running = mock.Mock(return_value=False)
        loader.run()

        loader.out.log.assert_has_calls(
            [
                mock.call("Starting up the Heroku Local server..."),
                mock.call("Ingesting dataset from some_experiment_id-data.zip..."),
                mock.call(
                    "Server is running on http://localhost:{}. Press Ctrl+C to exit.".format(
                        os.environ.get("base_port", 5000)
                    )
                ),
                mock.call("Terminating dataset load for experiment some_experiment_id"),
                mock.call("Cleaning up local Heroku process..."),
                mock.call("Local Heroku process terminated."),
            ]
        )

    def test_load_raises_on_nonexistent_id(self, loader):
        loader.app_id = "nonsense"
        loader.keep_running = mock.Mock(return_value=False)
        with pytest.raises(IOError):
            loader.run()

    def test_load_with_replay(self, replay_loader, export):
        replay_loader.run()

        replay_loader.out.log.assert_has_calls(
            [
                mock.call("Starting up the Heroku Local server..."),
                mock.call("Ingesting dataset from some_experiment_id-data.zip..."),
                mock.call(
                    "Server is running on http://localhost:{}. Press Ctrl+C to exit.".format(
                        os.environ.get("base_port", 5000)
                    )
                ),
                mock.call("Launching the experiment..."),
                mock.call("Launching replay browser..."),
                mock.call("Terminating dataset load for experiment some_experiment_id"),
                mock.call("Cleaning up local Heroku process..."),
                mock.call("Local Heroku process terminated."),
            ]
        )


class TestConstraints(object):
    @pytest.mark.slow
    def test_constraints_generation(self):
        from dallinger.deployment import ensure_constraints_file_presence

        tmp_path = tempfile.mkdtemp()
        (Path(tmp_path) / "requirements.txt").write_text("black")
        ensure_constraints_file_presence(tmp_path)
        constraints_file = Path(tmp_path) / "constraints.txt"
        # If not present a `constraints.txt` file will be generated
        assert constraints_file.exists()
        assert "toml" in constraints_file.read_text()

        # An existing file will be left untouched
        constraints_file.write_text("foobar")
        ensure_constraints_file_presence(tmp_path)
        assert constraints_file.read_text() == "foobar"
        shutil.rmtree(tmp_path)
