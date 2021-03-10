import mock
import os
import pytest

pytest_plugins = ["pytest_dallinger"]


@pytest.fixture(scope="module")
def check_firefox(request):
    if not request.config.getvalue("firefox"):
        pytest.skip("--firefox was not specified")


@pytest.fixture(scope="module")
def check_chrome(request):
    if not request.config.getvalue("chrome"):
        pytest.skip("--chrome was not specified")


@pytest.fixture(scope="module")
def check_chrome_headless(request):
    if not request.config.getvalue("chrome_headless"):
        pytest.skip("--chrome-headless was not specified")


@pytest.fixture(scope="module")
def check_webdriver(request):
    if not request.config.getvalue("webdriver"):
        pytest.skip("--webdriver was not specified")


@pytest.fixture(scope="module")
def check_phantomjs(request):
    if not request.config.getvalue("phantomjs"):
        pytest.skip("--phantomjs was not specified")


@pytest.fixture(scope="module")
def check_heroku(request):
    if not request.config.getvalue("heroku"):
        pytest.skip("--heroku was not specified")


@pytest.fixture(scope="module")
def check_runbot(request):
    if not request.config.getvalue("runbot"):
        pytest.skip("--runbot was not specified")


@pytest.fixture(scope="module")
def check_griduniverse(request):
    if not request.config.getvalue("griduniverse"):
        pytest.skip("--griduniverse was not specified")


@pytest.fixture(scope="module")
def check_mturkfull(request):
    if not request.config.getvalue("mturkfull"):
        pytest.skip("--mturkfull was not specified")


@pytest.fixture(scope="module")
def check_manual(request):
    if not request.config.getvalue("manual"):
        pytest.skip("--manual was not specified")


@pytest.fixture(scope="module")
def check_s3buckets(request):
    if not request.config.getvalue("s3buckets"):
        pytest.skip("--s3buckets was not specified")


@pytest.fixture(scope="class", autouse=True)
def reset_config():
    yield

    # Make sure extra parameters aren't kept between tests
    import dallinger.config

    dallinger.config.config = None


@pytest.fixture(scope="session", autouse=True)
def subprocess_coverage():
    # Set env var to trigger starting coverage for subprocesses
    coverage_path = os.path.dirname(os.path.dirname(__file__))
    os.environ["COVERAGE_PROCESS_START"] = os.path.join(coverage_path, ".coveragerc")
    os.environ["COVERAGE_FILE"] = os.path.join(coverage_path, ".coverage")


@pytest.fixture(scope="class")
def experiment_dir(root):
    os.chdir("tests/experiment")
    yield
    os.chdir(root)


@pytest.fixture
def experiment_dir_merged(experiment_dir, active_config):
    """A temp directory with files from the standard test experiment, merged
    with standard Dallinger files by the same process that occurs in production.
    """
    from dallinger.deployment import assemble_experiment_temp_dir
    from dallinger.deployment import ensure_constraints_file_presence

    current_dir = os.getcwd()
    ensure_constraints_file_presence(current_dir)
    destination = assemble_experiment_temp_dir(active_config)
    os.chdir(destination)
    yield
    os.chdir(current_dir)


@pytest.fixture(scope="class")
def bartlett_dir(root):
    os.chdir("demos/dlgr/demos/bartlett1932")
    yield
    os.chdir(root)


@pytest.fixture(scope="class")
def aws_creds():
    from dallinger.config import get_config

    config = get_config()
    if not config.ready:
        config.load()
    creds = {
        "aws_access_key_id": config.get("aws_access_key_id"),
        "aws_secret_access_key": config.get("aws_secret_access_key"),
    }
    return creds


@pytest.fixture
def custom_app_output():
    with mock.patch("dallinger.heroku.tools.check_output") as check_output:

        def my_check_output(cmd):
            if "auth:whoami" in cmd:
                return b"test@example.com"
            elif "config:get" in cmd:
                if "CREATOR" in cmd and "dlgr-my-uid" in cmd:
                    return b"test@example.com"
                elif "DALLINGER_UID" in cmd:
                    return cmd[-1].replace("dlgr-", "")
                return b""
            elif "apps" in cmd:
                return b"""[
{"name": "dlgr-my-uid",
    "created_at": "2018-01-01T12:00Z",
    "web_url": "https://dlgr-my-uid.herokuapp.com"},
{"name": "dlgr-another-uid",
    "created_at": "2018-01-02T00:00Z",
    "web_url": "https://dlgr-another-uid.herokuapp.com"}
]"""

        check_output.side_effect = my_check_output
        yield check_output


def pytest_addoption(parser):
    parser.addoption(
        "--webdriver",
        nargs="?",
        action="store",
        help="URL of selenium server including /wd/hub to run remote tests against",
        metavar="URL",
    )
    parser.addoption(
        "--runbot",
        action="store_true",
        help="Run an experiment using a bot during tests",
    )
    parser.addoption(
        "--manual",
        action="store_true",
        help="Run manual interactive tests during test run",
    )
    parser.addoption(
        "--mturkfull",
        action="store_true",
        default=False,
        help="Run comprehensive MTurk integration tests during test run",
    )
    parser.addoption(
        "--heroku", action="store_true", help="Run tests requiring heroku login"
    )
    parser.addoption(
        "--griduniverse",
        action="store_true",
        help="Run griduinverse tests and fail if not all pass",
    )
    parser.addoption(
        "--s3buckets",
        action="store_true",
        default=False,
        help="Run tests which create S3 buckets",
    )


def pytest_collection_modifyitems(config, items):
    run_slow = run_docker = False
    if config.getoption("--runslow"):
        # --runslow given in cli: do not skip slow tests
        run_slow = True
    if os.environ.get("RUN_DOCKER"):
        # RUN_DOCKER environment variable set: do not skip docker tests
        run_docker = True
    skip_slow = pytest.mark.skip(reason="need --runslow option to run")
    skip_docker = pytest.mark.skip(reason="need RUN_DOCKER environment variable")
    for item in items:
        if "slow" in item.keywords and not run_slow:
            item.add_marker(skip_slow)
        if "docker" in item.keywords and not run_docker:
            item.add_marker(skip_docker)
