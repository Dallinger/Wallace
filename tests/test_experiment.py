import pytest
import mock

from dallinger.models import Participant


def is_uuid(thing):
    return len(thing) == 36


@pytest.mark.usefixtures("active_config")
class TestExperimentBaseClass(object):
    @pytest.fixture
    def klass(self):
        from dallinger.experiment import Experiment

        return Experiment

    @pytest.fixture
    def exp(self, active_config, klass):
        # active_config.set("recruiter", "spy")
        return klass()

    @pytest.fixture
    def exp_with_session(self, klass, db_session):
        return klass(db_session)

    def test_recruiter_delegates(self, exp, active_config):
        with mock.patch("dallinger.experiment.recruiters") as mock_module:
            exp.recruiter
            mock_module.from_config.assert_called_once_with(active_config)

    def test_make_uuid_generates_random_id_if_passed_none(self, klass):
        assert is_uuid(klass.make_uuid(None))

    def test_make_uuid_generates_random_id_if_passed_nonuuid(self, klass):
        assert is_uuid(klass.make_uuid("None"))

    def test_make_uuid_echos_a_valid_uuid(self, klass):
        valid = "8a61fc2e-43ae-cc13-fd1e-aa8f676096cc"
        assert klass.make_uuid(valid) == valid

    def test_quorum_zero_by_default(self, exp):
        assert exp.quorum == 0

    def test_not_overrecruited_with_default_zero_quorum(self, exp):
        assert not exp.is_overrecruited(waiting_count=1)

    def test_is_overrecruited_if_waiting_exceeds_quorum(self, exp):
        exp.quorum = 1
        assert exp.is_overrecruited(waiting_count=2)

    def test_not_overrecruited_if_waiting_equal_to_quorum(self, exp):
        exp.quorum = 1
        assert not exp.is_overrecruited(waiting_count=1)

    def test_create_participant(self, exp_with_session):
        from dallinger.models import Participant

        assert len(Participant.query.filter(Participant.hit_id == "1").all()) == 0

        p = exp_with_session.create_participant(
            "1", "1", "1", "debug", entry_information={"some_key": "some_value"}
        )

        assert isinstance(p, Participant)
        assert p.hit_id == "1"
        assert p.worker_id == "1"
        assert p.assignment_id == "1"
        assert p.recruiter_id == "hotair"
        assert p.entry_information == {"some_key": "some_value"}
        assert len(Participant.query.filter(Participant.hit_id == "1").all()) == 1

    def test_create_participant_with_custom_class(self, exp_with_session):
        from dallinger.models import Participant

        class MyParticipant(Participant):
            pass

        exp_with_session.participant_constructor = MyParticipant
        p = exp_with_session.create_participant("1", "1", "1", "debug")

        assert isinstance(p, MyParticipant)

    def test_load_participant(self, exp, a):
        p = a.participant()
        assert exp.load_participant(p.assignment_id) == p

    def test_dashboard_fail(self, exp_with_session, a):
        p = a.participant()
        p2 = a.participant()
        n = a.node()
        n2 = a.node()
        n.fail()
        assert p.failed is False
        assert p2.failed is False
        assert n.failed is True
        assert n2.failed is False
        data = [
            {"id": p.id, "object_type": "Participant"},
            {"id": p2.id, "object_type": "Participant"},
            {"id": n.id, "object_type": "Node"},
            {"id": n2.id, "object_type": "Node"},
        ]
        result = exp_with_session.dashboard_fail(data)
        assert result == {"message": "Failed 1 Nodes, 2 Participants"}
        assert p.failed is True
        assert p2.failed is True
        assert n.failed is True
        assert n2.failed is True

    def test_normalize_entry_information_calls_recruiter(self, exp):
        with mock.patch(
            "dallinger.recruiters.Recruiter.normalize_entry_information"
        ) as normalizer:
            normalizer.side_effect = lambda *args: {
                "assignment_id": "A",
                "worker_id": "W",
                "hit_id": "H",
                "entry_information": args[-1],
            }
            exp.normalize_entry_information({"foo": "bar"})
            normalizer.assert_called_once_with({"foo": "bar"})

    def test_participant_task_completed_adds_group_qualification_if_group(
        self, active_config, exp
    ):
        active_config.set("group_name", "some group name")
        participant = mock.Mock(spec=Participant, worker_id="some worker id")

        exp.participant_task_completed(participant)

        assert (
            participant.recruiter.assign_experiment_qualifications.call_args_list
            == [
                mock.call(
                    worker_id="some worker id",
                    qualifications={
                        "TEST_EXPERIMENT_UID": "Experiment-specific qualification",
                        "some group name": "Experiment group qualification",
                    },
                )
            ]
        )

    def test_participant_task_completed_skips_assigning_qualification_if_so_configured(
        self, active_config, exp
    ):
        participant = mock.Mock(spec=Participant, worker_id="some worker id")
        active_config.set("assign_qualifications", False)

        exp.participant_task_completed(participant)

        participant.recruiter.assign_experiment_qualifications.assert_not_called()

    def test_participant_task_completed_skips_assigning_qualification_if_overrecruited(
        self, exp
    ):
        participant = mock.Mock(
            spec=Participant, worker_id="some worker id", status="overrecruited"
        )

        exp.participant_task_completed(participant)

        participant.recruiter.assign_experiment_qualifications.assert_not_called()
