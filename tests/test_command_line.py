#!/usr/bin/python
# -*- coding: utf-8 -*-
import click
import mock
import os
import re
import six
import subprocess
from time import sleep
from uuid import UUID

from click.testing import CliRunner
import pytest

import dallinger.command_line
from dallinger.command_line import report_idle_after
import dallinger.version


def found_in(name, path):
    return os.path.exists(os.path.join(path, name))


@pytest.fixture
def sleepless():
    # Use this fixture to ignore sleep() calls, for speed.
    with mock.patch("dallinger.command_line.time.sleep"):
        yield


@pytest.fixture
def browser():
    with mock.patch("dallinger.command_line.webbrowser") as mock_browser:
        yield mock_browser


@pytest.fixture
def heroku():
    from dallinger.heroku.tools import HerokuApp

    instance = mock.Mock(spec=HerokuApp)
    with mock.patch("dallinger.command_line.HerokuApp") as mock_app_class:
        mock_app_class.return_value = instance
        yield instance


@pytest.fixture
def data():
    with mock.patch("dallinger.command_line.data") as mock_data:
        mock_data.backup.return_value = "fake backup url"
        mock_bucket = mock.Mock()
        mock_key = mock.Mock()
        mock_key.generate_url.return_value = "fake restore url"
        mock_bucket.lookup.return_value = mock_key
        mock_data.user_s3_bucket.return_value = mock_bucket
        yield mock_data


@pytest.fixture
def mturk():
    with mock.patch("dallinger.command_line.MTurkService") as mock_mturk:
        mock_instance = mock.Mock()
        mock_instance.get_hits.return_value = [
            {"id": "hit-id-1"},
            {"id": "hit-id-2", "annotation": "exp-id-2"},
        ]
        mock_mturk.return_value = mock_instance
        yield mock_mturk


@pytest.mark.slow
@pytest.mark.usefixtures("bartlett_dir", "reset_sys_modules")
class TestVerify(object):
    @pytest.fixture
    def v_package(self):
        from dallinger.command_line.utils import verify_package

        return verify_package

    @pytest.fixture
    def v_directory(self):
        from dallinger.command_line.utils import verify_directory

        return verify_directory

    def test_verify(self):
        subprocess.check_call(["dallinger", "verify"])

    def test_large_float_payment(self, active_config, v_package):
        active_config.extend({"base_payment": 1.2342})
        assert v_package() is False

    def test_negative_payment(self, active_config, v_package):
        active_config.extend({"base_payment": -1.99})
        assert v_package() is False

    def test_too_big_returns_false(self, v_directory):
        with mock.patch(
            "dallinger.command_line.utils.ExperimentFileSource.size",
            new_callable=mock.PropertyMock,
        ) as size:
            size.return_value = 6000000  # 6 MB, so over the limit
            assert v_directory(max_size_mb=5) is False

    def test_under_limit_returns_true(self, v_directory):
        with mock.patch(
            "dallinger.command_line.utils.ExperimentFileSource.size",
            new_callable=mock.PropertyMock,
        ) as size:
            size.return_value = 4000000  # 4 MB, so under the limit
            assert v_directory(max_size_mb=5) is True


@pytest.mark.slow
class TestCommandLine(object):
    def test_dallinger_no_args(self):
        output = subprocess.check_output(["dallinger"])
        assert b"Usage: dallinger [OPTIONS] COMMAND [ARGS]" in output

    def test_log_empty(self):
        id = "dlgr-3b9c2aeb"
        assert click.BadParameter, subprocess.call(["dallinger", "logs", "--app", id])

    def test_log_no_flag(self):
        assert click.BadParameter, subprocess.call(["dallinger", "logs"])

    def test_deploy_empty(self):
        id = "dlgr-3b9c2aeb"
        assert click.BadParameter, subprocess.call(["dallinger", "deploy", "--app", id])

    def test_sandbox_empty(self):
        id = "dlgr-3b9c2aeb"
        assert click.BadParameter, subprocess.call(
            ["dallinger", "sandbox", "--app", id]
        )

    def test_verify_id_short_fails(self):
        id = "dlgr-3b9c2aeb"
        assert click.BadParameter, dallinger.commandline.verify_id(id)

    def test_empty_id_fails_verification(self):
        assert click.BadParameter, dallinger.commandline.verify_id(None)

    def test_new_uuid(self):
        output = subprocess.check_output(["dallinger", "uuid"])
        assert isinstance(UUID(output.strip().decode("utf8"), version=4), UUID)

    def test_dallinger_help(self):
        output = subprocess.check_output(["dallinger", "--help"])
        assert b"Commands:" in output

    def test_setup(self):
        subprocess.check_call(["dallinger", "setup"])
        subprocess.check_call(["dallinger", "setup"])


@pytest.mark.slow
class TestReportAfterIdleDecorator(object):
    def test_reports_timeout(self, active_config):
        @report_idle_after(1)
        def will_time_out():
            sleep(2)

        with mock.patch("dallinger.command_line.admin_notifier") as getter:
            mock_messenger = mock.Mock()
            getter.return_value = mock_messenger
            will_time_out()
            mock_messenger.send.assert_called_once()


@pytest.mark.slow
class TestOutput(object):
    @pytest.fixture
    def output(self):
        from dallinger.command_line import Output

        return Output()

    def test_outs(self, output):
        output.log("logging")
        output.error("an error")
        output.blather("blah blah blah")


class TestHeader(object):
    def test_header_contains_version_number(self):
        # Make sure header contains the version number.
        assert dallinger.version.__version__ in dallinger.command_line.header


@pytest.mark.usefixtures("bartlett_dir", "reset_sys_modules")
class TestDebugCommand(object):
    @pytest.fixture
    def debug(self):
        from dallinger.command_line import debug

        return debug

    @pytest.fixture
    def deployment(self):
        with mock.patch("dallinger.command_line.DebugDeployment") as mock_dbgr:
            yield mock_dbgr

    def test_fails_if_run_outside_experiment_dir(self, debug, deployment):
        exp_dir = os.getcwd()
        try:
            os.chdir("../..")
            result = CliRunner().invoke(debug, [])
        finally:
            os.chdir(exp_dir)

        deployment.assert_not_called()
        assert result.exit_code == 2
        assert "directory is not a valid Dallinger experiment" in result.output

    def test_bad_config_prevents_deployment(self, debug, deployment, active_config):
        with mock.patch("dallinger.command_line.utils.verify_config") as verify_config:
            verify_config.side_effect = ValueError()
            returned = CliRunner().invoke(debug, [])
        assert (
            "There are problems with the current experiment. Please check with dallinger verify."
            in returned.output
        )
        deployment.assert_not_called()

    def test_creates_debug_deployment(self, debug, deployment):
        CliRunner().invoke(debug, [])
        deployment.assert_called_once()


@pytest.mark.usefixtures("bartlett_dir", "active_config", "reset_sys_modules")
class TestSandboxAndDeploy(object):
    @pytest.fixture
    def sandbox(self):
        from dallinger.command_line import sandbox

        return sandbox

    @pytest.fixture
    def deploy(self):
        from dallinger.command_line import deploy

        return deploy

    @pytest.fixture
    def dsss(self):
        with mock.patch(
            "dallinger.command_line.deploy_sandbox_shared_setup"
        ) as mock_dsss:
            yield mock_dsss

    def test_fails_if_run_outside_experiment_dir(self, sandbox, dsss):
        exp_dir = os.getcwd()
        os.chdir("..")
        result = CliRunner().invoke(sandbox, [])
        os.chdir(exp_dir)

        dsss.assert_not_called()
        assert result.exit_code == 2
        assert "directory is not a valid Dallinger experiment" in result.output

    def test_uses_specified_app_id(self, sandbox, dsss):
        CliRunner().invoke(sandbox, ["--verbose", "--app", "some app id"])
        dsss.assert_called_once_with(
            app="some app id", verbose=True, log=mock.ANY, prelaunch_actions=[]
        )

    def test_works_with_no_app_id(self, sandbox, dsss):
        CliRunner().invoke(sandbox, ["--verbose"])
        dsss.assert_called_once_with(
            app=None, verbose=True, log=mock.ANY, prelaunch_actions=[]
        )

    def test_sandbox_puts_mode_in_config(self, sandbox, active_config, dsss):
        CliRunner().invoke(sandbox, ["--verbose"])
        assert active_config.get("mode") == "sandbox"

    def test_deploy_puts_mode_in_config(self, deploy, active_config, dsss):
        CliRunner().invoke(deploy, ["--verbose"])
        assert active_config.get("mode") == "live"

    def test_sets_logfile_to_dash_for_some_reason(self, sandbox, active_config, dsss):
        CliRunner().invoke(sandbox, ["--verbose"])
        assert active_config.get("logfile") == "-"

    def test_rejects_invalid_app_id(self, sandbox, dsss):
        result = CliRunner().invoke(sandbox, ["--verbose", "--app", "dlgr-some app id"])
        dsss.assert_not_called()
        assert result.exit_code == 2
        assert "The --app parameter requires the full UUID" in result.output

    def test_accepts_valid_archive_path(self, sandbox, tempdir, dsss):
        CliRunner().invoke(sandbox, ["--verbose", "--archive", tempdir])

        dsss.assert_called_once_with(
            app=None, verbose=True, log=mock.ANY, prelaunch_actions=[mock.ANY]
        )

    def test_rejects_invalid_archive_path(self, sandbox, dsss):
        CliRunner().invoke(sandbox, ["--verbose", "--archive", "nonexistent/path"])

        dsss.assert_not_called()


class TestLoad(object):
    @pytest.fixture
    def load(self):
        from dallinger.command_line import load

        return load

    @pytest.fixture
    def deployment(self):
        with mock.patch("dallinger.command_line.LoaderDeployment") as dep:
            yield dep

    def test_load_with_app_id(self, load, deployment):
        CliRunner().invoke(load, ["--app", "some app id", "--replay", "--verbose"])
        deployment.assert_called_once_with(
            "some app id", mock.ANY, True, {"replay": True}
        )


class TestSummary(object):
    @pytest.fixture
    def summary(self):
        from dallinger.command_line import summary

        return summary

    @pytest.fixture
    def patched_summary_route(self):
        response = mock.Mock()
        response.json.return_value = {
            u"completed": True,
            u"nodes_remaining": 0,
            u"required_nodes": 0,
            u"status": "success",
            u"summary": [["approved", 1], ["submitted", 1]],
            u"unfilled_networks": 0,
        }
        with mock.patch("dallinger.command_line.requests") as req:
            req.get.return_value = response
            yield req

    def test_summary(self, summary, patched_summary_route):
        result = CliRunner().invoke(summary, ["--app", "some app id"])
        assert "Yield: 50.00%" in result.output


@pytest.mark.usefixtures("bartlett_dir")
@pytest.mark.slow
class TestBot(object):
    @pytest.fixture
    def bot_command(self):
        from dallinger.command_line import bot

        return bot

    @pytest.fixture
    def mock_bot(self):
        bot = mock.Mock()
        with mock.patch("dallinger.command_line.bot_factory") as bot_factory:
            bot_factory.return_value = bot
            yield bot

    def test_bot_factory(self):
        from dallinger.command_line import bot_factory
        from dallinger.deployment import setup_experiment
        from dallinger.bots import BotBase

        setup_experiment(log=mock.Mock())
        bot = bot_factory("some url")
        assert isinstance(bot, BotBase)

    def test_bot_no_debug_url(self, bot_command, mock_bot):
        CliRunner().invoke(bot_command, ["--app", "some app id"])

        assert mock_bot.run_experiment.called

    def test_bot_with_debug_url(self, bot_command, mock_bot):
        CliRunner().invoke(bot_command, ["--app", "some app id", "--debug", "some url"])

        assert mock_bot.run_experiment.called


class TestQualify(object):
    @pytest.fixture
    def qualify(self):
        from dallinger.command_line import qualify

        return qualify

    @pytest.fixture
    def mturk(self):
        with mock.patch("dallinger.command_line.MTurkService") as mock_mturk:
            mock_results = [{"id": "some qid", "score": 1}]
            mock_instance = mock.Mock()
            mock_instance.get_workers_with_qualification.return_value = mock_results
            mock_mturk.return_value = mock_instance

            yield mock_instance

    def test_qualify_single_worker(self, qualify, mturk):
        qual_value = 1
        result = CliRunner().invoke(
            qualify,
            [
                "--qualification",
                "some qid",
                "--value",
                six.text_type(qual_value),
                "some worker id",
            ],
        )
        assert result.exit_code == 0
        mturk.assign_qualification.assert_called_once_with(
            "some qid", "some worker id", qual_value, notify=False
        )
        mturk.get_workers_with_qualification.assert_called_once_with("some qid")

    def test_uses_mturk_sandbox_if_specified(self, qualify):
        qual_value = 1
        with mock.patch("dallinger.command_line.MTurkService") as mock_mturk:
            mock_mturk.return_value = mock.Mock()
            CliRunner().invoke(
                qualify,
                [
                    "--sandbox",
                    "--qualification",
                    "some qid",
                    "--value",
                    six.text_type(qual_value),
                    "some worker id",
                ],
            )
            assert "sandbox=True" in str(mock_mturk.call_args_list[0])

    def test_raises_with_no_worker(self, qualify, mturk):
        qual_value = 1
        result = CliRunner().invoke(
            qualify,
            ["--qualification", "some qid", "--value", six.text_type(qual_value)],
        )
        assert result.exit_code != 0
        assert "at least one worker ID" in result.output

    def test_can_elect_to_notify_worker(self, qualify, mturk):
        qual_value = 1
        result = CliRunner().invoke(
            qualify,
            [
                "--qualification",
                "some qid",
                "--value",
                six.text_type(qual_value),
                "--notify",
                "some worker id",
            ],
        )
        assert result.exit_code == 0
        mturk.assign_qualification.assert_called_once_with(
            "some qid", "some worker id", qual_value, notify=True
        )

    def test_qualify_multiple_workers(self, qualify, mturk):
        qual_value = 1
        result = CliRunner().invoke(
            qualify,
            [
                "--qualification",
                "some qid",
                "--value",
                six.text_type(qual_value),
                "worker1",
                "worker2",
            ],
        )
        assert result.exit_code == 0
        mturk.assign_qualification.assert_has_calls(
            [
                mock.call(u"some qid", u"worker1", 1, notify=False),
                mock.call(u"some qid", u"worker2", 1, notify=False),
            ]
        )

    def test_use_qualification_name(self, qualify, mturk):
        qual_value = 1
        mturk.get_qualification_type_by_name.return_value = {"id": "some qid"}
        result = CliRunner().invoke(
            qualify,
            [
                "--qualification",
                "some qual name",
                "--value",
                six.text_type(qual_value),
                "--by_name",
                "some worker id",
            ],
        )
        assert result.exit_code == 0
        mturk.assign_qualification.assert_called_once_with(
            "some qid", "some worker id", qual_value, notify=False
        )
        mturk.get_workers_with_qualification.assert_called_once_with("some qid")

    def test_use_qualification_name_with_bad_name(self, qualify, mturk):
        qual_value = 1
        mturk.get_qualification_type_by_name.return_value = None
        result = CliRunner().invoke(
            qualify,
            [
                "--qualification",
                "some qual name",
                "--value",
                six.text_type(qual_value),
                "--by_name",
                "some worker id",
            ],
        )
        assert result.exit_code == 2
        assert 'No qualification with name "some qual name" exists.' in result.output


class TestEmailTest(object):
    @pytest.fixture
    def email_test(self):
        from dallinger.command_line import email_test

        return email_test

    @pytest.fixture
    def mailer(self):
        from dallinger.notifications import SMTPMailer

        mock_mailer = mock.Mock(spec=SMTPMailer)
        with mock.patch("dallinger.command_line.SMTPMailer") as klass:
            klass.return_value = mock_mailer
            yield mock_mailer

    def test_check_with_good_config(self, email_test, mailer, active_config):
        result = CliRunner().invoke(
            email_test,
        )
        mailer.send.assert_called_once()
        assert result.exit_code == 0

    def test_check_with_missing_value(self, email_test, mailer, active_config):
        active_config.extend({"smtp_username": u"???"})
        result = CliRunner().invoke(
            email_test,
        )
        mailer.send.assert_not_called()
        assert result.exit_code == 0


class TestCompensate(object):

    DO_IT = "Y\n"
    DO_NOT_DO_IT = "N\n"

    @pytest.fixture
    def compensate(self):
        from dallinger.command_line import compensate

        return compensate

    @pytest.fixture
    def mturkrecruiter(self):
        from dallinger.recruiters import MTurkRecruiter

        recruiter = mock.Mock(spec=MTurkRecruiter)
        recruiter.compensate_worker.return_value = {
            "hit": {
                "title": "HIT Title",
                "reward": "5.0",
                "worker_url": "http://example.com/hit",
            },
            "qualification": {"qualification key 1": "qualification value 1"},
            "email": {
                "subject": "The Subject",
                "sender": "from@example.com",
                "recipients": ["w@example.com"],
                "body": "The\nbody",
            },
        }
        return recruiter

    def test_compensate_with_notification(self, compensate, mturkrecruiter):
        with mock.patch("dallinger.command_line.by_name") as by_name:
            by_name.return_value = mturkrecruiter
            result = CliRunner().invoke(
                compensate,
                [
                    "--worker_id",
                    "some worker ID",
                    "--email",
                    "worker@example.com",
                    "--dollars",
                    "5.00",
                ],
                input=self.DO_IT,
            )

        assert result.exit_code == 0
        mturkrecruiter.compensate_worker.assert_called_once_with(
            worker_id=u"some worker ID",
            email="worker@example.com",
            dollars=5.0,
            notify=True,
        )

    def test_compensate_without_notification(self, compensate, mturkrecruiter):
        with mock.patch("dallinger.command_line.by_name") as by_name:
            by_name.return_value = mturkrecruiter
            result = CliRunner().invoke(
                compensate,
                ["--worker_id", "some worker ID", "--dollars", "5.0"],
                input=self.DO_IT,
            )

        assert result.exit_code == 0
        mturkrecruiter.compensate_worker.assert_called_once_with(
            worker_id=u"some worker ID", email=None, dollars=5.0, notify=False
        )

    def test_can_be_aborted_cleanly_after_warning(self, compensate, mturkrecruiter):
        result = CliRunner().invoke(
            compensate,
            ["--worker_id", "some worker ID", "--dollars", "5.0"],
            input=self.DO_NOT_DO_IT,
        )
        assert result.exit_code == 0
        mturkrecruiter.compensate_worker.assert_not_called()

    def test_traps_errors_and_forwards_message_portion(
        self, compensate, mturkrecruiter
    ):
        with mock.patch("dallinger.command_line.by_name") as by_name:
            by_name.return_value = mturkrecruiter
            mturkrecruiter.compensate_worker.side_effect = Exception("Boom!")
            result = CliRunner().invoke(
                compensate,
                ["--worker_id", "some worker ID", "--dollars", "5.0"],
                input=self.DO_IT,
            )
        assert result.exit_code == 0


class TestExtendMTurkHIT(object):

    DO_IT = "Y\n"
    DO_NOT_DO_IT = "N\n"

    @pytest.fixture
    def extend(self):
        from dallinger.command_line import extend_mturk_hit

        return extend_mturk_hit

    @pytest.fixture
    def mturk(self):
        with mock.patch("dallinger.command_line.MTurkService") as mock_mturk:
            mock_instance = mock.Mock()
            mock_instance.extend_hit.return_value = {
                "title": "HIT Title",
                "reward": "5.0",
                "worker_url": "http://example.com/hit",
            }

            mock_mturk.return_value = mock_instance

            yield mock_instance

    def test_extends_hit_by_assignments_and_duration(self, extend, mturk):
        result = CliRunner().invoke(
            extend,
            [
                "--hit_id",
                "some HIT ID",
                "--assignments",
                "3",
                "--duration_hours",
                "2.5",
                "--sandbox",
            ],
            input=self.DO_IT,
        )
        assert result.exit_code == 0
        mturk.extend_hit.assert_called_once_with(
            duration_hours=2.5, hit_id=u"some HIT ID", number=3
        )

    def test_duration_is_optional(self, extend, mturk):
        result = CliRunner().invoke(
            extend,
            ["--hit_id", "some HIT ID", "--assignments", "3", "--sandbox"],
            input=self.DO_IT,
        )
        assert result.exit_code == 0
        mturk.extend_hit.assert_called_once_with(
            duration_hours=None, hit_id=u"some HIT ID", number=3
        )

    def test_can_be_aborted_cleanly_after_warning(self, extend, mturk):
        result = CliRunner().invoke(
            extend,
            [
                "--hit_id",
                "some HIT ID",
                "--assignments",
                "3",
                "--duration_hours",
                "2.5",
                "--sandbox",
            ],
            input=self.DO_NOT_DO_IT,
        )
        assert result.exit_code == 0
        mturk.extend_hit.assert_not_called()


class TestRevoke(object):

    DO_IT = "Y\n"
    DO_NOT_DO_IT = "N\n"

    @pytest.fixture
    def revoke(self):
        from dallinger.command_line import revoke

        return revoke

    @pytest.fixture
    def mturk(self):
        with mock.patch("dallinger.command_line.MTurkService") as mock_mturk:
            mock_instance = mock.Mock()
            mock_instance.get_qualification_type_by_name.return_value = "some qid"
            mock_instance.get_workers_with_qualification.return_value = [
                {"id": "some qid", "score": 1}
            ]
            mock_mturk.return_value = mock_instance

            yield mock_instance

    def test_revoke_single_worker_by_qualification_id(self, revoke, mturk):
        result = CliRunner().invoke(
            revoke,
            [
                "--qualification",
                "some qid",
                "--reason",
                "some reason",
                "some worker id",
            ],
            input=self.DO_IT,
        )
        assert result.exit_code == 0
        mturk.revoke_qualification.assert_called_once_with(
            u"some qid", u"some worker id", u"some reason"
        )

    def test_can_be_aborted_cleanly_after_warning(self, revoke, mturk):
        result = CliRunner().invoke(
            revoke,
            [
                "--qualification",
                "some qid",
                "--reason",
                "some reason",
                "some worker id",
            ],
            input=self.DO_NOT_DO_IT,
        )
        assert result.exit_code == 0
        mturk.revoke_qualification.assert_not_called()

    def test_uses_mturk_sandbox_if_specified(self, revoke):
        with mock.patch("dallinger.command_line.MTurkService") as mock_mturk:
            mock_mturk.return_value = mock.Mock()
            CliRunner().invoke(
                revoke,
                [
                    "--sandbox",
                    "--qualification",
                    "some qid",
                    "--reason",
                    "some reason",
                    "some worker id",
                ],
                input=self.DO_IT,
            )
            assert "sandbox=True" in str(mock_mturk.call_args_list[0])

    def test_reason_has_a_default(self, revoke, mturk):
        result = CliRunner().invoke(
            revoke, ["--qualification", "some qid", "some worker id"], input=self.DO_IT
        )
        assert result.exit_code == 0
        mturk.revoke_qualification.assert_called_once_with(
            u"some qid",
            u"some worker id",
            u"Revoking automatically assigned Dallinger qualification",
        )

    def test_raises_with_no_worker(self, revoke, mturk):
        result = CliRunner().invoke(
            revoke, ["--qualification", "some qid"], input=self.DO_IT
        )
        assert result.exit_code != 0
        assert "at least one worker ID" in result.output

    def test_raises_with_no_qualification(self, revoke, mturk):
        result = CliRunner().invoke(revoke, [u"some worker id"], input=self.DO_IT)
        assert result.exit_code != 0
        assert "at least one worker ID" in result.output

    def test_revoke_for_multiple_workers(self, revoke, mturk):
        result = CliRunner().invoke(
            revoke,
            [
                "--qualification",
                "some qid",
                "--reason",
                "some reason",
                "worker1",
                "worker2",
            ],
            input=self.DO_IT,
        )
        assert result.exit_code == 0
        mturk.revoke_qualification.assert_has_calls(
            [
                mock.call(u"some qid", u"worker1", u"some reason"),
                mock.call(u"some qid", u"worker2", u"some reason"),
            ]
        )

    def test_use_qualification_name(self, revoke, mturk):
        mturk.get_qualification_type_by_name.return_value = {"id": "some qid"}
        result = CliRunner().invoke(
            revoke,
            [
                "--qualification",
                "some qual name",
                "--reason",
                "some reason",
                "--by_name",
                "some worker id",
            ],
            input=self.DO_IT,
        )
        assert result.exit_code == 0
        mturk.revoke_qualification.assert_called_once_with(
            u"some qid", u"some worker id", u"some reason"
        )

    def test_bad_qualification_name_shows_error(self, revoke, mturk):
        mturk.get_qualification_type_by_name.return_value = None
        result = CliRunner().invoke(
            revoke,
            [
                "--qualification",
                "some bad name",
                "--reason",
                "some reason",
                "--by_name",
                "some worker id",
            ],
            input=self.DO_IT,
        )
        assert result.exit_code == 2
        assert 'No qualification with name "some bad name" exists.' in result.output


class TestHibernate(object):
    @pytest.fixture
    def hibernate(self, sleepless):
        from dallinger.command_line import hibernate

        return hibernate

    def test_creates_backup(self, hibernate, heroku, data):
        CliRunner().invoke(hibernate, ["--app", "some-app-uid"])
        data.backup.assert_called_once_with("some-app-uid")

    def test_scales_down_dynos(self, hibernate, heroku, data):
        CliRunner().invoke(hibernate, ["--app", "some-app-uid"])
        heroku.scale_down_dynos.assert_called_once()

    def test_kills_addons(self, hibernate, heroku, data):
        CliRunner().invoke(hibernate, ["--app", "some-app-uid"])
        heroku.addon_destroy.assert_has_calls(
            [mock.call("heroku-postgresql"), mock.call("heroku-redis")]
        )


@pytest.mark.usefixtures("active_config")
class TestAwaken(object):
    @pytest.fixture
    def awaken(self, sleepless):
        from dallinger.command_line import awaken

        return awaken

    def test_creates_database_of_configured_size(
        self, awaken, heroku, data, active_config
    ):
        CliRunner().invoke(awaken, ["--app", "some-app-uid"])
        size = active_config.get("database_size")
        expected = mock.call("heroku-postgresql:{}".format(size))
        assert expected == heroku.addon.call_args_list[0]

    def test_adds_redis(self, awaken, heroku, data, active_config):
        active_config.set("redis_size", u"premium-2")
        CliRunner().invoke(awaken, ["--app", "some-app-uid"])
        assert mock.call("heroku-redis:premium-2") == heroku.addon.call_args_list[1]

    def test_restores_database_from_backup(self, awaken, heroku, data):
        CliRunner().invoke(awaken, ["--app", "some-app-uid"])
        heroku.restore.assert_called_once_with("fake restore url")

    def test_scales_up_dynos(self, awaken, heroku, data, active_config):
        web_count = active_config.get("num_dynos_web")
        worker_count = active_config.get("num_dynos_worker")
        size = active_config.get("dyno_type")
        active_config.set("clock_on", True)
        CliRunner().invoke(awaken, ["--app", "some-app-uid"])
        heroku.scale_up_dyno.assert_has_calls(
            [
                mock.call("web", web_count, size),
                mock.call("worker", worker_count, size),
                mock.call("clock", 1, size),
            ]
        )


class TestDestroy(object):
    @pytest.fixture
    def destroy(self):
        from dallinger.command_line import destroy

        return destroy

    @pytest.mark.slow
    def test_calls_destroy(self, destroy, heroku):
        CliRunner().invoke(
            destroy, ["--app", "some-app-uid", "--yes", "--no-expire-hit"]
        )
        heroku.destroy.assert_called_once()

    def test_destroy_expires_hits(self, destroy, heroku, mturk):
        CliRunner().invoke(destroy, ["--app", "some-app-uid", "--yes", "--expire-hit"])
        heroku.destroy.assert_called_once()
        mturk_instance = mturk.return_value
        mturk_instance.get_hits.assert_called_once()
        mturk_instance.expire_hit.assert_called()

    def test_destroy_no_expire_hits(self, destroy, heroku, mturk):
        CliRunner().invoke(
            destroy, ["--app", "some-app-uid", "--yes", "--no-expire-hit"]
        )
        heroku.destroy.assert_called_once()
        mturk_instance = mturk.return_value
        mturk_instance.get_hits.not_called()
        mturk_instance.expire_hit.not_called()

    def test_requires_confirmation(self, destroy, heroku):
        CliRunner().invoke(destroy, ["--app", "some-app-uid"])
        heroku.destroy.assert_not_called()

    def test_destroy_expire_uses_sandbox(self, destroy, heroku, mturk):
        CliRunner().invoke(
            destroy, ["--app", "some-app-uid", "--yes", "--expire-hit", "--sandbox"]
        )
        assert "sandbox=True" in str(mturk.call_args_list[0])
        mturk_instance = mturk.return_value
        mturk_instance.get_hits.assert_called_once()
        mturk_instance.expire_hit.assert_called()


class TestLogs(object):
    @pytest.fixture
    def logs(self):
        from dallinger.command_line import logs

        return logs

    def test_opens_logs(self, logs, heroku):
        CliRunner().invoke(logs, ["--app", "some-app-uid"])
        heroku.open_logs.assert_called_once()


class TestMonitor(object):
    def _twice(self):
        count = [2]

        def countdown():
            if count[0]:
                count[0] -= 1
                return True
            return False

        return countdown

    @pytest.fixture
    def command_line_check_call(self):
        with mock.patch("dallinger.command_line.check_call") as call:
            yield call

    @pytest.fixture
    def summary(self):
        with mock.patch("dallinger.command_line.get_summary") as sm:
            sm.return_value = "fake summary"
            yield sm

    @pytest.fixture
    def two_summary_checks(self):
        countdown = self._twice()
        counter_factory = mock.Mock(return_value=countdown)
        with mock.patch(
            "dallinger.command_line._keep_running", new_callable=counter_factory
        ):
            yield

    @pytest.fixture
    def monitor(self, sleepless, summary, two_summary_checks):
        from dallinger.command_line import monitor

        return monitor

    def test_opens_browsers(self, monitor, heroku, browser, command_line_check_call):
        heroku.dashboard_url = "fake-dashboard-url"
        CliRunner().invoke(monitor, ["--app", "some-app-uid"])
        browser.open.assert_has_calls(
            [
                mock.call("fake-dashboard-url"),
                mock.call("https://requester.mturk.com/mturk/manageHITs"),
            ]
        )

    def test_calls_open_with_db_uri(
        self, monitor, heroku, browser, command_line_check_call
    ):
        heroku.db_uri = "fake-db-uri"
        CliRunner().invoke(monitor, ["--app", "some-app-uid"])
        command_line_check_call.assert_called_once_with(["open", "fake-db-uri"])

    def test_shows_summary_in_output(
        self, monitor, heroku, browser, command_line_check_call
    ):
        heroku.db_uri = "fake-db-uri"
        result = CliRunner().invoke(monitor, ["--app", "some-app-uid"])

        assert len(re.findall("fake summary", result.output)) == 2

    def test_raises_on_null_app_id(
        self, monitor, heroku, browser, command_line_check_call
    ):
        heroku.db_uri = "fake-db-uri"
        result = CliRunner().invoke(monitor, ["--app", None])
        assert result.exit_code == 2
        assert "Select an experiment using the --app parameter." in result.output


class TestHits(object):
    @pytest.fixture
    def output(self):
        with mock.patch("dallinger.command_line.Output") as mock_data:
            output_instance = mock.Mock()
            mock_data.return_value = output_instance
            yield output_instance

    @pytest.fixture
    def hits(self):
        from dallinger.command_line import hits

        return hits

    @pytest.fixture
    def expire(self):
        from dallinger.command_line import expire

        return expire

    def test_hits(self, hits, mturk):
        result = CliRunner().invoke(hits, ["--app", "exp-id-2"])
        assert result.exit_code == 0
        mturk_instance = mturk.return_value
        mturk_instance.get_hits.assert_called_once()

    def test_uses_mturk_sandbox_if_specified(self, hits, mturk):
        CliRunner().invoke(hits, ["--sandbox", "--app", "exp-id-2"])
        assert "sandbox=True" in str(mturk.call_args_list[0])

    def test_expire(self, expire, mturk):
        result = CliRunner().invoke(expire, ["--app", "exp-id-2"])
        assert result.exit_code == 0
        mturk_instance = mturk.return_value
        mturk_instance.get_hits.assert_called_once()
        mturk_instance.expire_hit.assert_called()

    def test_expire_no_hits(self, expire, mturk, output):
        mturk_instance = mturk.return_value
        mturk_instance.get_hits.return_value = []
        result = CliRunner().invoke(expire, ["--app", "exp-id-2"])
        assert result.exit_code == 1
        mturk_instance.get_hits.assert_called_once()
        mturk_instance.expire_hit.assert_not_called()
        assert output.log.call_count == 2

        output.log.assert_has_calls(
            [
                mock.call("No hits found for this application."),
                mock.call(
                    "If this experiment was run in the MTurk sandbox, use: "
                    "`dallinger expire --sandbox --app exp-id-2`"
                ),
            ]
        )

    def test_expire_no_hits_sandbox(self, expire, mturk, output):
        mturk_instance = mturk.return_value
        mturk_instance.get_hits.return_value = []
        result = CliRunner().invoke(expire, ["--app", "exp-id-2", "--sandbox"])
        assert result.exit_code == 1
        mturk_instance.get_hits.assert_called_once()
        mturk_instance.expire_hit.assert_not_called()
        output.log.assert_called_once_with("No hits found for this application.")

    def test_expire_with_failure(self, expire, mturk, output):
        mturk_instance = mturk.return_value

        def mturk_raiser(*args, **kwargs):
            from dallinger.mturk import MTurkServiceException

            raise MTurkServiceException()

        mturk_instance.expire_hit.side_effect = mturk_raiser
        result = CliRunner().invoke(expire, ["--app", "exp-id-2"])
        assert result.exit_code == 1
        mturk_instance.get_hits.assert_called_once()
        mturk_instance.expire_hit.call_count = 2
        assert output.log.call_count == 1
        assert "Could not expire 2 hits:" in str(output.log.call_args_list[0])


class TestApps(object):
    @pytest.fixture
    def console_output(self):
        with mock.patch("dallinger.command_line.Output") as mock_data:
            output_instance = mock.Mock()
            mock_data.return_value = output_instance
            yield output_instance

    @pytest.fixture
    def tabulate(self):
        with mock.patch("tabulate.tabulate") as tabulate:
            yield tabulate

    @pytest.fixture
    def apps(self):
        from dallinger.command_line import apps

        return apps

    def test_apps(
        self, apps, custom_app_output, console_output, tabulate, active_config
    ):
        active_config["team"] = u"fake team"
        result = CliRunner().invoke(apps)
        assert result.exit_code == 0
        custom_app_output.assert_has_calls(
            [
                mock.call(["heroku", "auth:whoami"]),
                mock.call(["heroku", "apps", "--json", "--team", "fake team"]),
                mock.call(["heroku", "config:get", "CREATOR", "--app", "dlgr-my-uid"]),
                mock.call(
                    ["heroku", "config:get", "DALLINGER_UID", "--app", "dlgr-my-uid"]
                ),
                mock.call(
                    ["heroku", "config:get", "CREATOR", "--app", "dlgr-another-uid"]
                ),
                mock.call(["heroku", "auth:whoami"]),
            ]
        )
        tabulate.assert_called_with(
            [["my-uid", "2018-01-01T12:00Z", "https://dlgr-my-uid.herokuapp.com"]],
            ["UID", "Started", "URL"],
            tablefmt=u"psql",
        )
