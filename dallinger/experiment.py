"""The base experiment class."""

from __future__ import print_function
from __future__ import unicode_literals

from cached_property import cached_property
from collections import Counter
from collections import OrderedDict
from contextlib import contextmanager
from functools import wraps
import datetime
import inspect
from importlib import import_module
import logging
from operator import itemgetter
import os
import random
import requests
import sys
import time
import uuid
import warnings

from sqlalchemy import and_
from sqlalchemy import create_engine
from sqlalchemy import distinct
from sqlalchemy import func
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.orm.exc import NoResultFound, MultipleResultsFound

from dallinger import recruiters
from dallinger.config import get_config, LOCAL_CONFIG
from dallinger.config import initialize_experiment_package
from dallinger.data import Data
from dallinger.data import export
from dallinger.data import is_registered
from dallinger.data import load as data_load
from dallinger.data import find_experiment_export
from dallinger.data import ingest_zip
from dallinger.db import init_db, db_url
from dallinger import models
from dallinger.models import Network, Node, Info, Transformation, Participant, Vector
from dallinger.heroku.tools import HerokuApp
from dallinger.information import Gene, Meme, State
from dallinger.nodes import Agent, Source, Environment
from dallinger.transformations import Compression, Response
from dallinger.transformations import Mutation, Replication
from dallinger.utils import struct_to_html


from dallinger.networks import Empty

logger = logging.getLogger(__file__)


def exp_class_working_dir(meth):
    @wraps(meth)
    def new_meth(self, *args, **kwargs):
        try:
            config = get_config()
            orig_path = os.getcwd()
            new_path = os.path.dirname(sys.modules[self.__class__.__module__].__file__)
            os.chdir(new_path)
            # Override configs
            config.register_extra_parameters()
            config.load_from_file(LOCAL_CONFIG)
            return meth(self, *args, **kwargs)
        finally:
            config.clear()
            os.chdir(orig_path)

    return new_meth


class Experiment(object):
    """Define the structure of an experiment."""

    app_id = None
    # Optional Redis channel to create and subscribe to on launch. Note that if
    # you define a channel, you probably also want to override the send()
    # method, since this is where messages from Redis will be sent.
    channel = None
    exp_config = None
    replay_path = "/"

    #: Constructor for Participant objects. Callable returning an instance of
    #: :attr:`~dallinger.models.Participant` or a sub-class. Used by
    #: :func:`~dallinger.experiment.Experiment.create_participant`.
    participant_constructor = Participant

    def __init__(self, session=None):
        """Create the experiment class. Sets the default value of attributes."""

        #: Boolean, determines whether the experiment logs output when
        #: running. Default is True.
        self.verbose = True

        #: String, the name of the experiment. Default is "Experiment
        #: title".
        self.task = "Experiment title"

        #: session, the experiment's connection to the database.
        self.session = session

        #: int, the number of practice networks (see
        #: :attr:`~dallinger.models.Network.role`). Default is 0.
        self.practice_repeats = 0

        #: int, the number of non practice networks (see
        #: :attr:`~dallinger.models.Network.role`). Default is 0.
        self.experiment_repeats = 0

        #: int, the number of participants
        #: required to move from the waiting room to the experiment.
        #: Default is 0 (no waiting room).
        self.quorum = 0

        #: int, the number of participants
        #: requested when the experiment first starts. Default is 1.
        self.initial_recruitment_size = 1

        #: dictionary, the classes Dallinger can make in response
        #: to front-end requests. Experiments can add new classes to this
        #: dictionary.
        self.known_classes = {
            "Agent": Agent,
            "Compression": Compression,
            "Environment": Environment,
            "Gene": Gene,
            "Info": Info,
            "Meme": Meme,
            "Mutation": Mutation,
            "Node": Node,
            "Replication": Replication,
            "Response": Response,
            "Source": Source,
            "State": State,
            "Transformation": Transformation,
        }

        #: dictionary, the properties of this experiment that are exposed
        #: to the public over an AJAX call
        if not hasattr(self, "public_properties"):
            # Guard against subclasses replacing this with a @property
            self.public_properties = {}

        if session:
            self.configure()

        try:
            location = type(self).__module__
            parent, experiment_module = location.rsplit(".", 1)
            module = import_module(parent + ".jupyter")
        except (ImportError, ValueError):
            try:
                from .jupyter import ExperimentWidget

                self.widget = ExperimentWidget(self)
            except ImportError:
                self.widget = None
        else:
            self.widget = module.ExperimentWidget(self)

    @classmethod
    def extra_parameters(cls):
        """Override this classmethod to register new config variables. It is
        called during config load. See
        :ref:`Extra Configuration <extra-configuration>` for an example.
        """
        pass

    def configure(self):
        """Load experiment configuration here"""
        pass

    @property
    def background_tasks(self):
        """An experiment may define functions or methods to be started as
        background tasks upon experiment launch.
        """
        return []

    @cached_property
    def recruiter(self):
        """Reference to a Recruiter, the Dallinger class that recruits
        participants.
        """
        return recruiters.from_config(get_config())

    @property
    def qualifications(self):
        """All the qualifications we want to assign to a worker.

        By default, workers will always be assigned one qualification specific
        to the experiment run. The property `group_qualifications` defines
        additional qualifications to assign.
        """
        experiment_qualification_desc = "Experiment-specific qualification"
        config = get_config()
        quals = {config.get("id"): experiment_qualification_desc}
        quals.update(self.group_qualifications)

        return quals

    @property
    def group_qualifications(self):
        """Qualifications that likely span more than one experiment run.

        By default, the config.txt variable `group_name` is treated
        as a comma-delimited list of additional qualifications to assign.
        """
        group_qualification_desc = "Experiment group qualification"
        config = get_config()
        qualification_names = config.get("group_name", "").split(",")

        return {name: group_qualification_desc for name in qualification_names}

    @property
    def qualification_active(self):
        config = get_config()
        return bool(config.get("assign_qualifications"))

    def is_overrecruited(self, waiting_count):
        """Returns True if the number of people waiting is in excess of the
        total number expected, indicating that this and subsequent users should
        skip the experiment. A quorum value of 0 means we don't limit
        recruitment, and always return False.
        """
        if not self.quorum:
            return False
        return waiting_count > self.quorum

    def send(self, raw_message):
        """socket interface implementation, and point of entry for incoming
        Redis messages.

        param raw_message is a string with a channel prefix, for example:

            'shopping:{"type":"buy","color":"blue","quantity":"2"}'
        """
        pass

    def setup(self):
        """Create the networks if they don't already exist."""
        if not self.networks():
            for _ in range(self.practice_repeats):
                network = self.create_network()
                network.role = "practice"
                self.session.add(network)
            for _ in range(self.experiment_repeats):
                network = self.create_network()
                network.role = "experiment"
                self.session.add(network)
            self.session.commit()

    def create_network(self):
        """Return a new network."""
        return Empty()

    def networks(self, role="all", full="all"):
        """All the networks in the experiment."""
        if full not in ["all", True, False]:
            raise ValueError(
                "full must be boolean or all, it cannot be {}".format(full)
            )

        if full == "all":
            if role == "all":
                return Network.query.all()
            else:
                return Network.query.filter_by(role=role).all()
        else:
            if role == "all":
                return Network.query.filter_by(full=full).all()
            else:
                return Network.query.filter(
                    and_(Network.role == role, Network.full == full)
                ).all()

    def get_network_for_participant(self, participant):
        """Find a network for a participant.

        If no networks are available, None will be returned. By default
        participants can participate only once in each network and participants
        first complete networks with `role="practice"` before doing all other
        networks in a random order.

        """
        key = participant.id
        networks_with_space = (
            Network.query.filter_by(full=False).order_by(Network.id).all()
        )
        networks_participated_in = [
            node.network_id
            for node in Node.query.with_entities(Node.network_id)
            .filter_by(participant_id=participant.id)
            .all()
        ]

        legal_networks = [
            net for net in networks_with_space if net.id not in networks_participated_in
        ]

        if not legal_networks:
            self.log("No networks available, returning None", key)
            return None

        self.log(
            "{} networks out of {} available".format(
                len(legal_networks), (self.practice_repeats + self.experiment_repeats)
            ),
            key,
        )

        legal_practice_networks = [
            net for net in legal_networks if net.role == "practice"
        ]
        if legal_practice_networks:
            chosen_network = legal_practice_networks[0]
            self.log(
                "Practice networks available."
                "Assigning participant to practice network {}.".format(
                    chosen_network.id
                ),
                key,
            )
        else:
            chosen_network = self.choose_network(legal_networks, participant)
            self.log(
                "No practice networks available."
                "Assigning participant to experiment network {}".format(
                    chosen_network.id
                ),
                key,
            )
        return chosen_network

    def choose_network(self, networks, participant):
        return random.choice(networks)

    def create_node(self, participant, network):
        """Create a node for a participant."""
        return Node(network=network, participant=participant)

    def add_node_to_network(self, node, network):
        """Add a node to a network.

        This passes `node` to :func:`~dallinger.models.Network.add_node()`.

        """
        network.add_node(node)

    def normalize_entry_information(self, entry_information):
        """Accepts a dictionary with information about a recruited user. Returns
        a dictionary containing data the needed to create or load a Dallinger
        Participant. The returned data should include valid ``assignment_id``,
        ``worker_id``, and ``hit_id`` values. It may also include an
        ``entry_information`` key which should contain a transformed
        ``entry_information`` dict which will be stored for newly created
        participants.

        By default, the extraction of these values is delegated to the
        recruiter's `normalize_entry_information` method.

        Returning a dictionary without valid ``hit_id``, ``assignment_id``, or
        ``worker_id`` will generally result in an exception.
        """
        entry_data = self.recruiter.normalize_entry_information(entry_information)
        # We need an assignment_id in order to create a participant
        return entry_data

    def create_participant(
        self,
        worker_id,
        hit_id,
        assignment_id,
        mode,
        recruiter_name=None,
        fingerprint_hash=None,
        entry_information=None,
    ):
        """Creates and returns a new participant object. Uses
        :attr:`~dallinger.experiment.Experiment.participant_constructor` as the
        constructor.

        :param worker_id: the recruiter Worker Id
        :type worker_id: str
        :param hit_id: the recruiter HIT Id
        :type hit_id: str
        :param assignment_id: the recruiter Assignment Id
        :type assignment_id: str
        :param mode: the application mode
        :type mode: str
        :param recruiter_name: the recruiter name
        :type recruiter_name: str
        :param fingerprint_hash: the user's fingerprint
        :type fingerprint_hash: str
        :param entry_information: a JSON serializable data structure containing
                                  additional participant entry information
        :returns: A :attr:`~dallinger.models.Participant` instance
        """
        if not recruiter_name:
            recruiter = self.recruiter
            if recruiter:
                recruiter_name = recruiter.nickname

        participant = self.participant_constructor(
            recruiter_id=recruiter_name,
            worker_id=worker_id,
            assignment_id=assignment_id,
            hit_id=hit_id,
            mode=mode,
            fingerprint_hash=fingerprint_hash,
            entry_information=entry_information,
        )
        self.session.add(participant)
        return participant

    def load_participant(self, assignment_id):
        """Returns a participant object looked up by assignment_id.

        Intended to allow a user to resume a session in a running experiment.

        :param assignment_id: the recruiter Assignment Id
        :type assignment_id: str
        :returns: A ``Participant`` instance or ``None`` if there is not a
                  single matching participant.
        """
        try:
            return Participant.query.filter_by(assignment_id=assignment_id).one()
        except (NoResultFound, MultipleResultsFound):
            return None

    def data_check(self, participant):
        """Check that the data are acceptable.

        Return a boolean value indicating whether the `participant`'s data is
        acceptable. This is meant to check for missing or invalid data. This
        check will be run once the `participant` completes the experiment. By
        default performs no checks and returns True. See also,
        :func:`~dallinger.experiments.Experiment.attention_check`.

        """
        return True

    def bonus(self, participant):
        """The bonus to be awarded to the given participant.

        Return the value of the bonus to be paid to `participant`. By default
        returns 0.

        """
        return 0

    def bonus_reason(self):
        """The reason offered to the participant for giving the bonus.

        Return a string that will be included in an email sent to the
        `participant` receiving a bonus. By default it is "Thank you for
        participating! Here is your bonus."

        """
        return "Thank for participating! Here is your bonus."

    def exit_info_for(self, participant):
        """An experiment can return a dictionary of infomation that will
        be shown to the participant at the very last point in their
        lifecycle, if the HIT is not submitted to an external recruitment
        service for submission.

        For complete control over the exit page, a customized version of
        the ``exit_recruiter.html`` template can be included in the experient
        directory, and this will override the default provided by Dallinger.

        :param participant: the ``Participant`` instance for which to calculate
            an exit value
        :returns: ``dict`` which may be rendered to the worker as an HTML table
            when they submit their assigment.
        """
        return {
            "Assignment ID": participant.assignment_id,
            "HIT ID": participant.hit_id,
            "Base Pay": participant.base_pay,
            "Bonus": participant.bonus,
        }

    def attention_check(self, participant):
        """Check if participant performed adequately.

        Return a boolean value indicating whether the `participant`'s data is
        acceptable. This is mean to check the participant's data to determine
        that they paid attention. This check will run once the *participant*
        completes the experiment. By default performs no checks and returns
        True. See also :func:`~dallinger.experiments.Experiment.data_check`.

        """
        return True

    def participant_task_completed(self, participant):
        """Called when an experiment task is finished and submitted, and prior
        to data and attendance checks.

        Assigns the qualifications to the Participant, via their recruiter.
        These will include one Qualification for the experiment
        ID, and others for the configured group_name, if it's been set.

        Overrecruited participants don't receive qualifications, since they
        haven't actually completed the experiment. This allows them to remain
        eligible for future runs.

        :param participant: the ``Participant`` instance
        """
        if not self.qualification_active:
            logger.info("Qualification assignment is globally disabled; ignoring.")
            return

        if participant.status == "overrecruited":
            return

        participant.recruiter.assign_experiment_qualifications(
            worker_id=participant.worker_id, qualifications=self.qualifications
        )

    def submission_successful(self, participant):
        """Run when a participant's experiment submission passes data
        and attendence checks.

        :param participant: the ``Participant`` instance
        """
        pass

    def recruit(self):
        """Recruit participants to the experiment as needed.

        This method runs whenever a participant successfully completes the
        experiment (participants who fail to finish successfully are
        automatically replaced). By default it recruits 1 participant at a time
        until all networks are full.

        """
        if not self.networks(full=False):
            self.log("All networks full: closing recruitment", "-----")
            self.recruiter.close_recruitment()

    def log(self, text, key="?????", force=False):
        """Print a string to the logs."""
        if force or self.verbose:
            print(">>>> {} {}".format(key, text))
            sys.stdout.flush()

    def log_summary(self):
        """Log a summary of all the participants' status codes."""
        participants = Participant.query.with_entities(Participant.status).all()
        counts = Counter([p.status for p in participants])
        sorted_counts = sorted(counts.items(), key=itemgetter(0))
        self.log("Status summary: {}".format(str(sorted_counts)))
        return sorted_counts

    def save(self, *objects):
        """Add all the objects to the session and commit them.

        This only needs to be done for networks and participants.

        """
        if len(objects) > 0:
            self.session.add_all(objects)
        self.session.commit()

    def node_post_request(self, participant, node):
        """Run when a request to make a node is complete."""
        pass

    def node_get_request(self, node=None, nodes=None):
        """Run when a request to get nodes is complete."""
        pass

    def vector_post_request(self, node, vectors):
        """Run when a request to connect is complete."""
        pass

    def vector_get_request(self, node, vectors):
        """Run when a request to get vectors is complete."""
        pass

    def info_post_request(self, node, info):
        """Run when a request to create an info is complete."""
        pass

    def info_get_request(self, node, infos):
        """Run when a request to get infos is complete."""
        pass

    def transmission_post_request(self, node, transmissions):
        """Run when a request to transmit is complete."""
        pass

    def transmission_get_request(self, node, transmissions):
        """Run when a request to get transmissions is complete."""
        pass

    def transformation_post_request(self, node, transformation):
        """Run when a request to transform an info is complete."""
        pass

    def transformation_get_request(self, node, transformations):
        """Run when a request to get transformations is complete."""
        pass

    def fail_participant(self, participant):
        """Fail all the nodes of a participant."""
        participant_nodes = Node.query.filter_by(
            participant_id=participant.id, failed=False
        ).all()

        for node in participant_nodes:
            node.fail()

    def data_check_failed(self, participant):
        """What to do if a participant fails the data check.

        Runs when `participant` has failed
        :func:`~dallinger.experiments.Experiment.data_check`. By default calls
        :func:`~dallinger.experiments.Experiment.fail_participant`.

        """
        self.fail_participant(participant)

    def attention_check_failed(self, participant):
        """What to do if a participant fails the attention check.

        Runs when `participant` has failed the
        :func:`~dallinger.experiments.Experiment.attention_check`. By default calls
        :func:`~dallinger.experiments.Experiment.fail_participant`.

        """
        self.fail_participant(participant)

    def assignment_abandoned(self, participant):
        """What to do if a participant abandons the hit.

        This runs when a notification from AWS is received indicating that
        `participant` has run out of time. Calls
        :func:`~dallinger.experiments.Experiment.fail_participant`.

        """
        self.fail_participant(participant)

    def assignment_returned(self, participant):
        """What to do if a participant returns the hit.

        This runs when a notification from AWS is received indicating that
        `participant` has returned the experiment assignment. Calls
        :func:`~dallinger.experiments.Experiment.fail_participant`.

        """
        self.fail_participant(participant)

    def assignment_reassigned(self, participant):
        """What to do if the assignment assigned to a participant is
        reassigned to another participant while the first participant
        is still working.

        This runs when a participant is created with the same assignment_id
        as another participant if the earlier participant still has the status
        "working". Calls :func:`~dallinger.experiments.Experiment.fail_participant`.

        """
        self.fail_participant(participant)

    @exp_class_working_dir
    def run(self, exp_config=None, app_id=None, bot=False, **kwargs):
        """Deploy and run an experiment.

        The exp_config object is either a dictionary or a
        ``localconfig.LocalConfig`` object with parameters
        specific to the experiment run grouped by section.
        """
        import dallinger as dlgr

        app_id = self.make_uuid(app_id)

        if bot:
            kwargs["recruiter"] = "bots"

        self.app_id = app_id
        self.exp_config = exp_config or kwargs
        self.update_status("Starting")
        try:
            if self.exp_config.get("mode") == "debug":
                dlgr.command_line.debug.callback(
                    verbose=True, bot=bot, proxy=None, exp_config=self.exp_config
                )
            else:
                dlgr.deployment.deploy_sandbox_shared_setup(
                    dlgr.command_line.log,
                    app=app_id,
                    verbose=self.verbose,
                    exp_config=self.exp_config,
                )
        except Exception:
            self.update_status("Errored")
            raise
        else:
            self.update_status("Running")
        self._await_completion()
        self.update_status("Retrieving data")
        data = self.retrieve_data()
        self.update_status("Completed")
        return data

    def collect(self, app_id, exp_config=None, bot=False, **kwargs):
        """Collect data for the provided experiment id.

        The ``app_id`` parameter must be a valid UUID.
        If an existing data file is found for the UUID it will
        be returned, otherwise - if the UUID is not already registered -
        the experiment will be run and data collected.

        See :meth:`~Experiment.run` method for other parameters.
        """
        try:
            results = data_load(app_id)
            self.log(
                "Data found for experiment {}, retrieving.".format(app_id),
                key="Retrieve:",
            )
            return results
        except IOError:
            self.log(
                "Could not fetch data for id: {}, checking registry".format(app_id),
                key="Retrieve:",
            )

        exp_config = exp_config or {}
        if is_registered(app_id):
            raise RuntimeError(
                "The id {} is registered, ".format(app_id)
                + "but you do not have permission to access to the data"
            )
        elif kwargs.get("mode") == "debug" or exp_config.get("mode") == "debug":
            raise RuntimeError("No remote or local data found for id {}".format(app_id))

        try:
            assert isinstance(uuid.UUID(app_id, version=4), uuid.UUID)
        except (ValueError, AssertionError):
            raise ValueError("Invalid UUID supplied {}".format(app_id))

        self.log(
            "{} appears to be a new experiment id, running experiment.".format(app_id),
            key="Retrieve:",
        )
        return self.run(exp_config, app_id, bot, **kwargs)

    @classmethod
    def make_uuid(cls, app_id=None):
        """Generates a new UUID.
        This is a class method and can be called as `Experiment.make_uuid()`.
        Takes an optional `app_id` which is converted to a string and, if it
        is a valid UUID, returned.
        """
        try:
            if app_id and isinstance(uuid.UUID(str(app_id), version=4), uuid.UUID):
                return str(app_id)
        except (ValueError, AssertionError):
            pass
        return str(uuid.UUID(int=random.getrandbits(128)))

    def experiment_completed(self):
        """Checks the current state of the experiment to see whether it has
        completed. This makes use of the experiment server `/summary` route,
        which in turn uses :meth:`~Experiment.is_complete`.
        """
        heroku_app = HerokuApp(self.app_id)
        status_url = "{}/summary".format(heroku_app.url)
        data = {}
        try:
            resp = requests.get(status_url)
            data = resp.json()
        except (ValueError, requests.exceptions.RequestException):
            logger.exception("Error fetching experiment status.")
        logger.debug("Current application state: {}".format(data))
        return data.get("completed", False)

    def _await_completion(self):
        # Debug runs synchronously, but in live mode we need to loop and check
        # experiment status
        if self.exp_config.get("mode") != "debug":
            self.log("Waiting for experiment to complete.", "")
            while not self.experiment_completed():
                time.sleep(30)
        return True

    def retrieve_data(self):
        """Retrieves and saves data from a running experiment"""
        local = False
        if self.exp_config.get("mode") == "debug":
            local = True
        filename = export(self.app_id, local=local)
        logger.debug("Data exported to %s" % filename)
        return Data(filename)

    def end_experiment(self):
        """Terminates a running experiment"""
        if self.exp_config.get("mode") != "debug":
            HerokuApp(self.app_id).destroy()
        return True

    def events_for_replay(self, session=None, target=None):
        """Returns an ordered list of "events" for replaying.
        Experiments may override this method to provide custom
        replay logic. The "events" returned by this method will be passed
        to :meth:`~Experiment.replay_event`. The default implementation
        simply returns all :class:`~dallinger.models.Info` objects in the
        order they were created.
        """
        if session is None:
            session = self.session
        return session.query(Info).order_by(Info.creation_time)

    def replay_event(self, event):
        """Stub method to replay an event returned by
        :meth:`~Experiment.events_for_replay`.
        Experiments must override this method to provide replay support.
        """
        pass

    def replay_start(self):
        """Stub method for starting an experiment replay.
        Experiments must override this method to provide replay support.
        """
        pass

    def replay_finish(self):
        """Stub method for ending an experiment replay.
        Experiments must override this method to provide replay support.
        """
        pass

    def replay_started(self):
        """Returns `True` if an experiment replay has started."""
        return True

    def is_complete(self):
        """Method for custom determination of experiment completion.
        Experiments should override this to provide custom experiment
        completion logic. Returns `None` to use the experiment server
        default logic, otherwise should return `True` or `False`.
        """
        return None

    def monitoring_panels(self, **kw):
        """Provides monitoring dashboard sidebar panels.

        :param \**kw: arguments passed in from the request
        :returns: An ``OrderedDict()`` mapping panel titles to HTML strings
                  to render in the dashboard sidebar.
        """  # noqa
        stats = self.monitoring_statistics(**kw)
        panels = OrderedDict()
        for tab in stats:
            panels[tab] = struct_to_html(stats[tab])
        return panels

    def monitoring_statistics(self, **kw):
        """The default data used for the monitoring panels

        :param \**kw: arguments passed in from the request
        :returns: An ``OrderedDict()`` mapping panel titles to data structures
                  describing the experiment state.
        """  # noqa
        participants = Participant.query
        nodes = Node.query
        infos = Info.query

        stats = OrderedDict()
        stats["Participants"] = OrderedDict(
            (
                ("working", participants.filter_by(status="working").count()),
                ("abandoned", participants.filter_by(status="abandoned").count()),
                ("returned", participants.filter_by(status="returned").count()),
                ("approved", participants.filter_by(status="approved").count()),
            )
        )

        # Count up our networks by role
        network_roles = self.session.query(Network.role, func.count(Network.role))
        network_counts = network_roles.group_by(Network.role).all()
        failed_networks = network_roles.filter(Network.failed == True)  # noqa
        failed_counts = dict(failed_networks.group_by(Network.role).all())
        network_stats = {}
        for role, count in network_counts:
            network_stats[role] = OrderedDict(
                (
                    ("count", count),
                    ("failed", failed_counts.get(role, 0)),
                )
            )
        stats["Networks"] = network_stats

        stats["Nodes"] = OrderedDict(
            (
                ("count", nodes.count()),
                ("failed", nodes.filter_by(failed=True).count()),
            )
        )

        stats["Infos"] = OrderedDict(
            (
                ("count", infos.count()),
                ("failed", infos.filter_by(failed=True).count()),
            )
        )

        if kw.get("transformations"):
            transformations = Transformation.query
            stats["transformations"] = OrderedDict(
                (
                    ("count", transformations.count()),
                    ("failed", transformations.filter_by(failed=True).count()),
                )
            )

        return stats

    def network_structure(self, **kw):
        network_ids = {i[0] for i in self.session.query(distinct(Network.id)).all()}
        if "network_roles" in kw:
            network_ids = {
                i[0]
                for i in self.session.query(distinct(Network.id)).filter(
                    Network.role.in_(kw["network_roles"])
                )
            }
        if "network_ids" in kw:
            network_ids = network_ids.intersection(int(v) for v in kw["network_ids"])

        jnetworks = [
            n.__json__()
            for n in Network.query.filter(Network.id.in_(network_ids)).all()
        ]
        if "collapsed" in kw:
            # Collapsed view shows Source nodes only
            jnodes = [
                n.__json__()
                for n in Source.query.filter(Node.network_id.in_(network_ids)).all()
            ]
            jinfos = jparticipants = jtransformations = jvectors = []
        else:
            jnodes = [
                n.__json__()
                for n in Node.query.filter(Node.network_id.in_(network_ids)).all()
            ]
            jinfos = [
                n.__json__()
                for n in Info.query.filter(Info.network_id.in_(network_ids)).all()
            ]
            # We don't filter participants because they aren't directly connected to specific networks
            jparticipants = [n.__json__() for n in Participant.query.all()]
            jtransformations = []
            if kw.get("transformations"):
                jtransformations = [
                    n.__json__()
                    for n in Transformation.query.filter(
                        Transformation.network_id.in_(network_ids)
                    ).all()
                ]

            jvectors = [
                {
                    "origin_id": v.origin_id,
                    "destination_id": v.destination_id,
                    "id": v.id,
                    "failed": v.failed,
                }
                for v in Vector.query.filter(Vector.network_id.in_(network_ids)).all()
            ]

        return {
            "networks": jnetworks,
            "nodes": jnodes,
            "vectors": jvectors,
            "infos": jinfos,
            "participants": jparticipants,
            "trans": jtransformations,
        }

    def node_visualization_options(self):
        """Provides custom vis.js configuration options for the
        Network Monitoring Dashboard.

        :returns: A dict with `vis.js option values <https://visjs.github.io/vis-network/docs/network/#options>`__
        """
        return {}

    def node_visualization_html(self, object_type, obj_id):
        """Returns a string with custom HTML visualization for a given object
        referenced by the object base type and id.

        :param object_type: The base object class name, e.g. ``Network``, ``Node``, ``Info``, ``Participant``, etc.
        :type object_type: str
        :param id: The ``id`` of the object
        :type id: int

        :returns: A valid HTML string to be inserted into the monitoring dashboard
        """

        model = getattr(models, object_type, None)
        if model is not None:
            obj = self.session.query(model).get(int(obj_id))
            if getattr(obj, "visualization_html", None):
                return obj.visualization_html
        return ""

    def table_data(self, **kw):
        """Generates DataTablesJS data and configuration for the experiment. The data
        is compiled from the models' ``__json__`` methods, and can be customized by either
        overriding this method or using the ``json_data`` method on the model to return
        additional serializable data.

        :param \**kw: arguments passed in from the request. The ``model_type`` parameter
                      takes a ``str`` or iterable and queries all objects of those types,
                      ordered by ``id``.
        :returns: Returns a ``dict`` with DataTablesJS data and configuration, filters using
                  arbitrary keyword arguments. Should contain ``data`` and ``columns`` keys
                  at least, with ``columns`` containing data for all fields on all returned
                  objects.
        """  # noqa
        rows = []
        found_columns = set()
        columns = []
        model_types = kw.get("model_type", ["Participant"])
        if hasattr(model_types, "strip"):
            model_types = [model_types]

        for model_type in model_types:
            model = getattr(models, model_type, None)
            for obj in model.query.order_by(model.id).all():
                data = obj.__json__()
                # Add participant worker_id to data, we normally leave it out of
                # JSON renderings
                if model_type == "Participant":
                    data["worker_id"] = obj.worker_id
                rows.append(data)
                for key in data:
                    if key not in found_columns:
                        columns.append({"name": key, "data": key})
                        found_columns.add(key)

        # Make sure every row has an entry for every column
        for col in found_columns:
            for row in rows:
                if col not in row:
                    row[col] = None

        return {
            "data": rows,
            "columns": columns,
        }

    def dashboard_database_actions(self):
        """Returns a sequence of custom actions for the database dashboard. Each action
        must have a ``title`` and a ``name`` corresponding to a method on the
        experiment class.

        The named methods should take a single ``data`` argument
        which will be a list of dicts representing the datatables rendering of
        a Dallinger model object. The named methods should return a ``dict``
        containing a ``"message"`` which will be displayed in the dashboard.

        Returns a single action referencing the
        :func:`~dallinger.experiment.Experiment.dashboard_fail`
        method by default.
        """
        return [{"name": "dashboard_fail", "title": "Fail Selected"}]

    def dashboard_fail(self, data):
        """Marks matching non-failed items as failed. Items are looked up by
        ``id`` and ``object_type`` (e.g. ``"Participant"``).

        :param data: A list of dicts representing model items to be marked as failed.
                     Each must have an ``id`` and an ``object_type``
        :type object_type: list

        :returns: Returns a ``dict`` with a ``"message"`` string indicating how
                  many items were successfully marked as failed.
        """
        counts = {}
        for entry in data:
            obj_id = entry.get("id")
            object_type = entry.get("object_type")
            model = getattr(models, object_type, None)
            if model is not None:
                obj = self.session.query(model).get(int(obj_id))
                if obj is not None and not obj.failed:
                    obj.fail()
                    counts[object_type] = counts.get(object_type, 0) + 1
        if not counts:
            return {"message": "No nodes found to fail"}
        return {
            "message": "Failed {}".format(
                ", ".join("{} {}s".format(c, t) for t, c in sorted(counts.items()))
            )
        }

    @property
    def usable_replay_range(self):
        """The range of times that represent the active part of the experiment"""
        return self._replay_range

    @contextmanager
    def restore_state_from_replay(
        self, app_id, session, zip_path=None, **configuration_options
    ):
        # We need to fake dallinger_experiment to point at the current experiment
        module = sys.modules[type(self).__module__]
        if sys.modules.get("dallinger_experiment", module) != module:
            logger.warning("dallinger_experiment is already set, updating")
        sys.modules["dallinger_experiment"] = module

        # Load the configuration system and globals
        config = get_config()
        # Manually load extra parameters and ignore errors
        try:
            from dallinger_experiment.experiment import extra_parameters

            try:
                extra_parameters()
                config._module_params_loaded = True
            except KeyError:
                pass
        except ImportError:
            pass

        config.load()
        self.app_id = self.original_app_id = app_id
        self.session = session
        self.exp_config = config

        # The replay index is initialised to 1970 as that is guaranteed
        # to be before any experiment Info objects
        self._replay_time_index = datetime.datetime(1970, 1, 1, 1, 1, 1)

        # Create a second database session so we can load the full history
        # of the experiment to be replayed and selectively import events
        # into the main database
        specific_db_url = db_url + "-import-" + app_id
        import_engine = create_engine(specific_db_url)
        try:
            # Clear the temporary storage and import it
            init_db(drop_all=True, bind=import_engine)
        except Exception:
            create_db_engine = create_engine(db_url)
            conn = create_db_engine.connect()
            conn.execute("COMMIT;")
            conn.execute(
                'CREATE DATABASE "{}"'.format(specific_db_url.rsplit("/", 1)[1])
            )
            conn.close()
            import_engine = create_engine(specific_db_url)
            init_db(drop_all=True, bind=import_engine)

        self.import_session = scoped_session(
            sessionmaker(autocommit=False, autoflush=True, bind=import_engine)
        )

        # Find the real data for this experiment
        if zip_path is None:
            zip_path = find_experiment_export(app_id)
        if zip_path is None:
            msg = 'Dataset export for app id "{}" could not be found.'
            raise IOError(msg.format(app_id))

        print("Ingesting dataset from {}...".format(os.path.basename(zip_path)))
        ingest_zip(zip_path, engine=import_engine)
        self._replay_range = tuple(
            self.import_session.query(
                func.min(Info.creation_time), func.max(Info.creation_time)
            )
        )[0]
        # We apply the configuration options we were given and yield
        # the scrubber function into the context manager, so within the
        # with experiment.restore_state_from_replay(...): block the configuration
        # options are correctly set
        with config.override(configuration_options, strict=True):
            self.replay_start()
            yield Scrubber(self, session=self.import_session)
            self.replay_finish()

        # Clear up global state
        self.import_session.rollback()
        self.import_session.close()
        session.rollback()
        session.close()
        config._reset(register_defaults=True)
        del sys.modules["dallinger_experiment"]

    def revert_to_time(self, session, target):
        # We do not support going back in time
        raise NotImplementedError

    def _ipython_display_(self):
        """Display Jupyter Notebook widget"""
        from IPython.display import display

        display(self.widget)

    def update_status(self, status):
        if self.widget is not None:
            self.widget.status = status

    def jupyter_replay(self, *args, **kwargs):
        from ipywidgets import widgets
        from IPython.display import display

        try:
            sys.modules["dallinger_experiment"]._jupyter_cleanup()
        except (KeyError, AttributeError):
            pass
        replay = self.restore_state_from_replay(*args, **kwargs)
        scrubber = replay.__enter__()
        scrubber.build_widget()
        replay_widget = widgets.VBox([self.widget, scrubber.widget])
        # Scrub to start of experiment and re-render the main widget
        scrubber(self.usable_replay_range[0])
        self.widget.render()
        display(replay_widget)
        # Defer the cleanup until this function is re-called by
        # keeping a copy of the function on the experiment module
        # This allows us to effectively detect the cell being
        # re-run as there doesn't seem to be a cleanup hook for widgets
        # displayed as part of a cell that is being re-rendered

        def _jupyter_cleanup():
            replay.__exit__(None, None, None)

        sys.modules["dallinger_experiment"]._jupyter_cleanup = _jupyter_cleanup


class Scrubber(object):
    def __init__(self, experiment, session):
        self.experiment = experiment
        self.session = session
        self.realtime = False

    def __call__(self, time):
        """Scrub to a point in the experiment replay, given by time
        which is a datetime object."""
        if self.experiment._replay_time_index > time:
            self.experiment.revert_to_time(session=self.session, target=time)
        events = self.experiment.events_for_replay(
            session=self.session, target=time
        ).all()
        for event in events:
            if event.creation_time <= self.experiment._replay_time_index:
                # Skip events we've already handled
                continue
            if event.creation_time > time:
                # Stop once we get future events
                break
            self.experiment.replay_event(event)
            self.experiment._replay_time_index = event.creation_time
        # Override app_id to allow exports to be created that don't
        # overwrite the original dataset
        self.experiment.app_id = "{}_{}".format(
            self.experiment.original_app_id, time.isoformat()
        )

    def in_realtime(self, callback=None):
        exp_start, exp_end = self.experiment.usable_replay_range
        replay_offset = time.time()
        current = self.experiment._replay_time_index
        if current < exp_start:
            current = exp_start
        self.realtime = True
        # Disable the scrubbing slider
        self.widget.children[0].disabled = True
        try:
            while current < exp_end:
                now = time.time()
                seconds = now - replay_offset
                current = current + datetime.timedelta(seconds=seconds)
                self(current)
                if callable(callback):
                    try:
                        callback()
                    except StopIteration:
                        return
                replay_offset = now
        finally:
            self.realtime = False
            self.widget.children[0].disabled = False

    def build_widget(self):
        from ipywidgets import widgets

        start, end = self.experiment.usable_replay_range
        options = []
        current = start
        while current <= end:
            # Never display microseconds
            options.append((current.replace(microsecond=0).time().isoformat(), current))
            current += datetime.timedelta(seconds=1)
            # But we need to keep microseconds in the first value, so we don't go before
            # the experiment start when scrubbing backwards
            current = current.replace(microsecond=0)
        scrubber = widgets.SelectionSlider(
            description="Current time",
            options=options,
            disabled=False,
            continuous_update=False,
        )

        def advance(change):
            if self.realtime:
                # We're being driven in realtime, the advancement
                # here is just to keep the UI in sync
                return
            old_status = self.experiment.widget.status
            self.experiment.widget.status = "Updating"
            self.experiment.widget.render()
            self(change["new"])
            self.experiment.widget.status = old_status
            self.experiment.widget.render()

        scrubber.observe(advance, "value")

        def realtime_callback():
            self.experiment.widget.render()
            try:
                scrubber.value = self.experiment._replay_time_index.replace(
                    microsecond=0
                )
            except Exception:
                # The scrubber is an approximation of the current time, we shouldn't
                # bail out if it can't be updated (for example at experiment bounds)
                pass
            if not self.realtime:
                raise StopIteration()

        play_button = widgets.ToggleButton(
            value=False,
            description="",
            disabled=False,
            tooltip="Play back in realtime",
            icon="play",
        )

        def playback(change):
            import threading

            if change["new"]:
                thread = threading.Thread(
                    target=self.in_realtime, kwargs={"callback": realtime_callback}
                )
                thread.start()
            else:
                self.realtime = False

        play_button.observe(playback, "value")

        self.widget = widgets.HBox(children=[scrubber, play_button])
        return self.widget

    def _ipython_display_(self):
        """Display Jupyter Notebook widget"""
        from IPython.display import display

        self.build_widget()
        display(self.widget())


def is_experiment_class(cls):
    return (
        inspect.isclass(cls) and issubclass(cls, Experiment) and cls is not Experiment
    )


def load():
    """Load the active experiment."""
    initialize_experiment_package(os.getcwd())
    try:
        try:
            from dallinger_experiment import experiment
        except ImportError:
            try:
                from dallinger_experiment import dallinger_experiment as experiment
            except ImportError:
                import dallinger_experiment as experiment

        classes = inspect.getmembers(experiment, is_experiment_class)

        preferred_class = os.environ.get("EXPERIMENT_CLASS_NAME", None)
        if preferred_class is not None:
            try:
                return dict(classes)[preferred_class]
            except KeyError:
                raise ImportError(
                    "No experiment named {} was found".format(preferred_class)
                )

        if len(classes) > 1:
            for name, c in classes:
                if "Experiment" in c.__bases__[0].__name__:
                    warnings.warn(
                        UserWarning(
                            "More than one potential experiment class found but no EXPERIMENT_CLASS_NAME environment variable. Picking {} from {}.".format(
                                name, [n for (n, cls) in classes]
                            )
                        ),
                        stacklevel=3,
                    )
                    return c
            raise ImportError(
                "No direct experiment subclass found in {}".format(
                    [n for (n, cls) in classes]
                )
            )
        elif len(classes) == 0:
            raise ImportError("No experiment classes found")
        else:
            return classes[0][1]
    except ImportError:
        logger.error("Could not import experiment.")
        raise
