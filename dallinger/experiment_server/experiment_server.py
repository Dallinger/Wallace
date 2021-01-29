""" This module provides the backend Flask server that serves an experiment. """

from datetime import datetime
import gevent
from json import dumps
from json import loads
import os
import re

from flask import (
    abort,
    Flask,
    render_template,
    request,
    Response,
    send_from_directory,
    url_for,
)
from flask_login import LoginManager
from jinja2 import TemplateNotFound
from rq import Queue
from sqlalchemy.orm.exc import NoResultFound, MultipleResultsFound
from sqlalchemy import exc
from sqlalchemy import func
from sqlalchemy.sql.expression import true
from psycopg2.extensions import TransactionRollbackError

from dallinger import db
from dallinger import experiment
from dallinger import models
from dallinger.config import get_config
from dallinger import recruiters
from dallinger.notifications import admin_notifier
from dallinger.notifications import MessengerError

from . import dashboard
from .replay import ReplayBackend
from .worker_events import worker_function
from .utils import (
    crossdomain,
    nocache,
    ValidatesBrowser,
    error_page,
    error_response,
    success_response,
    ExperimentError,
)


# Initialize the Dallinger database.
session = db.session
redis_conn = db.redis_conn

# Connect to the Redis queue for notifications.
q = Queue(connection=redis_conn)
WAITING_ROOM_CHANNEL = "quorum"

app = Flask("Experiment_Server")


@app.before_first_request
def _config():
    app.secret_key = app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY")
    config = get_config()
    if not config.ready:
        config.load()
    if config.get("dashboard_password", None):
        app.config["ADMIN_USER"] = dashboard.User(
            userid=config.get("dashboard_user", "admin"),
            password=config.get("dashboard_password"),
        )

    return config


def Experiment(args):
    klass = experiment.load()
    return klass(args)


# Load the experiment's extra routes, if any.
try:
    from dallinger_experiment.experiment import extra_routes
except ImportError:
    pass
else:
    app.register_blueprint(extra_routes)

# Ideally, we'd only load recruiter routes if the recruiter is active, but
# it turns out this is complicated, so for now we always register our
# primary recruiter's route:
app.register_blueprint(recruiters.mturk_routes)

# Load dashboard routes and login setup
app.register_blueprint(dashboard.dashboard)
login = LoginManager(app)
login.login_view = "dashboard.login"
login.request_loader(dashboard.load_user_from_request)
login.user_loader(dashboard.load_user)
login.unauthorized_handler(dashboard.unauthorized)
app.config["dashboard_tabs"] = dashboard.dashboard_tabs

"""Basic routes."""


@app.route("/")
def index():
    """Index route"""
    html = (
        "<html><head></head><body><h1>Dallinger Experiment in progress</h1>"
        "<p><a href={}>Dashboard</a></p></body></html>".format(
            url_for("dashboard.index")
        )
    )

    return html


@app.route("/robots.txt")
def static_robots_txt():
    """Serve robots.txt from static file."""
    return send_from_directory("static", "robots.txt")


@app.route("/favicon.ico")
def static_favicon():
    return send_from_directory("static", "favicon.ico", mimetype="image/x-icon")


@app.errorhandler(ExperimentError)
def handle_exp_error(exception):
    """Handle errors by sending an error page."""
    app.logger.error(
        "%s (%s) %s", exception.value, exception.errornum, str(dict(request.args))
    )
    return error_page(error_type=exception.value)


"""Define functions for handling requests."""


@app.teardown_request
def shutdown_session(_=None):
    """Rollback and close session at end of a request."""
    session.remove()
    db.logger.debug("Closing Dallinger DB session at flask request end")


@app.context_processor
def inject_experiment():
    """Inject experiment and enviroment variables into the template context."""
    exp = Experiment(session)
    return dict(experiment=exp, env=os.environ)


@app.route("/error-page", methods=["POST", "GET"])
def render_error():
    request_data = request.form.get("request_data")
    participant_id = request.form.get("participant_id")
    participant = None
    if participant_id:
        participant = models.Participant.query.get(participant_id)
    return error_page(participant=participant, request_data=request_data)


hit_error_template = """Dear experimenter,

This is an automated email from Dallinger. You are receiving this email because
a recruited participant has been unable to complete the experiment due to
a bug.

The application id is: {app_id}

The information about the failed HIT is recorded in the database in the
Notification table, with assignment_id {assignment_id}.

To see the logs, use the command "dallinger logs --app {app_id}"
To pause the app, use the command "dallinger hibernate --app {app_id}"
To destroy the app, use the command "dallinger destroy --app {app_id}"


The Dallinger dev. team.
"""


@app.route("/handle-error", methods=["POST"])
def handle_error():
    request_data = request.form.get("request_data")
    error_feedback = request.form.get("error_feedback")
    error_type = request.form.get("error_type")
    error_text = request.form.get("error_text")
    worker_id = request.form.get("worker_id")
    assignment_id = request.form.get("assignment_id")
    participant_id = request.form.get("participant_id")
    hit_id = request.form.get("hit_id")
    participant = None

    completed = False
    details = {"request_data": {}}

    if request_data:
        try:
            request_data = loads(request_data)
        except ValueError:
            request_data = {}
        details["request_data"] = request_data

        try:
            data = loads(request_data.get("data", "null")) or request_data
        except ValueError:
            data = request_data

        if not participant_id and "participant_id" in data:
            participant_id = data["participant_id"]
        if not worker_id and "worker_id" in data:
            worker_id = data["worker_id"]
        if not assignment_id and "assignment_id" in data:
            assignment_id = data["assignment_id"]
        if not hit_id and "hit_id" in data:
            hit_id = data["hit_id"]

    if participant_id:
        try:
            participant_id = int(participant_id)
        except (ValueError, TypeError):
            participant_id = None

    details["feedback"] = error_feedback
    details["error_type"] = error_type
    details["error_text"] = error_text

    if participant_id is None and worker_id:
        participants = (
            session.query(models.Participant).filter_by(worker_id=worker_id).all()
        )
        if participants:
            participant = participants[0]
            if not assignment_id:
                assignment_id = participant.assignment_id

    if participant_id is None and assignment_id:
        participants = (
            session.query(models.Participant).filter_by(worker_id=assignment_id).all()
        )
        if participants:
            participant = participants[0]
            participant_id = participant.id
            if not worker_id:
                worker_id = participant.worker_id

    if participant_id is not None:
        _worker_complete(participant_id)
        completed = True

    details["request_data"].update(
        {"worker_id": worker_id, "hit_id": hit_id, "participant_id": participant_id}
    )

    notif = models.Notification(
        assignment_id=assignment_id or "unknown",
        event_type="ExperimentError",
        details=details,
    )
    session.add(notif)
    session.commit()

    config = _config()
    message = {
        "subject": "Error during HIT.",
        "body": hit_error_template.format(
            app_id=config.get("id", "unknown"), assignment_id=assignment_id or "unknown"
        ),
    }
    db.logger.debug("Reporting HIT error...")
    messenger = admin_notifier(config)
    try:
        messenger.send(**message)
    except MessengerError as ex:
        db.logger.exception(ex)

    return render_template(
        "error-complete.html",
        completed=completed,
        contact_address=config.get("contact_email_on_error"),
        hit_id=hit_id,
    )


"""Define routes for managing an experiment and the participants."""


@app.route("/launch", methods=["POST"])
def launch():
    """Launch the experiment."""
    try:
        exp = Experiment(db.init_db(drop_all=False))
    except Exception as ex:
        return error_response(
            error_text="Failed to load experiment in /launch: {}".format(str(ex)),
            status=500,
            simple=True,
        )
    try:
        exp.log("Launching experiment...", "-----")
    except IOError as ex:
        return error_response(
            error_text="IOError writing to experiment log: {}".format(str(ex)),
            status=500,
            simple=True,
        )

    try:
        recruitment_details = exp.recruiter.open_recruitment(
            n=exp.initial_recruitment_size
        )
        session.commit()
    except Exception as e:
        return error_response(
            error_text="Failed to open recruitment, check experiment server log "
            "for details: {}".format(str(e)),
            status=500,
            simple=True,
        )

    for task in exp.background_tasks:
        try:
            gevent.spawn(task)
        except Exception:
            return error_response(
                error_text="Failed to spawn task on launch: {}, ".format(task)
                + "check experiment server log for details",
                status=500,
                simple=True,
            )

    if _config().get("replay", False):
        try:
            task = ReplayBackend(exp)
            gevent.spawn(task)
        except Exception:
            return error_response(
                error_text="Failed to launch replay task for experiment."
                "check experiment server log for details",
                status=500,
                simple=True,
            )

    # If the experiment defines a channel, subscribe the experiment to the
    # redis communication channel:
    if exp.channel is not None:
        try:
            from dallinger.experiment_server.sockets import chat_backend

            chat_backend.subscribe(exp, exp.channel)
        except Exception:
            return error_response(
                error_text="Failed to subscribe to chat for channel on launch "
                + "{}".format(exp.channel)
                + ", check experiment server log for details",
                status=500,
                simple=True,
            )

    message = "\n".join(
        (
            "Initial recruitment list:\n{}".format(
                "\n".join(recruitment_details["items"])
            ),
            "Additional details:\n{}".format(recruitment_details["message"]),
        )
    )

    return success_response(recruitment_msg=message)


@app.route("/ad", methods=["GET"])
@nocache
def advertisement():
    """
    This is the url we give for the ad for our 'external question'.  The ad has
    to display two different things: This page will be called from within
    mechanical turk, with url arguments hitId, assignmentId, and workerId.
    If the worker has not yet accepted the hit:
        These arguments will have null values, we should just show an ad for
        the experiment.
    If the worker has accepted the hit:
        These arguments will have appropriate values and we should enter the
        person in the database and provide a link to the experiment popup.
    """
    config = _config()

    # Browser rule validation, if configured:
    browser = ValidatesBrowser(config)
    if not browser.is_supported(request.user_agent.string):
        raise ExperimentError("browser_type_not_allowed")

    entry_information = request.args.to_dict()
    app_id = config.get("id", "unknown")
    exp = Experiment(session)
    entry_data = exp.normalize_entry_information(entry_information)

    hit_id = entry_data.get("hit_id")
    assignment_id = entry_data.get("assignment_id")
    worker_id = entry_data.get("worker_id")

    if not (hit_id and assignment_id):
        raise ExperimentError("hit_assign_worker_id_not_set_by_recruiter")

    if worker_id is not None:
        # Check if this workerId has completed the task before
        already_participated = (
            models.Participant.query.filter(
                models.Participant.worker_id == worker_id
            ).first()
            is not None
        )

        if already_participated:
            raise ExperimentError("already_did_exp_hit")

    recruiter_name = request.args.get("recruiter")
    if recruiter_name:
        recruiter = recruiters.by_name(recruiter_name)
    else:
        recruiter = recruiters.from_config(config)
        recruiter_name = recruiter.nickname

    # Participant has not yet agreed to the consent. They might not
    # even have accepted the HIT.
    return render_template(
        "ad.html",
        recruiter=recruiter_name,
        hitid=hit_id,
        assignmentid=assignment_id,
        workerid=worker_id,
        mode=config.get("mode"),
        app_id=app_id,
        query_string=request.query_string.decode(),
    )


@app.route("/recruiter-exit", methods=["GET"])
@nocache
def recriter_exit():
    """Display an exit page defined by the Participant's Recruiter.
    The Recruiter may in turn delegate to the Experiment for additional
    values to display.
    """
    participant_id = request.args.get("participant_id")
    if participant_id is None:
        return error_response(
            error_type="/recruiter-exit GET: param participant_id is required",
            status=400,
        )
    participant = models.Participant.query.get(participant_id)
    if participant is None:
        return error_response(
            error_type="/recruiter-exit GET: no participant found for ID {}".format(
                participant_id
            ),
            status=404,
        )

    # Get the recruiter from the participant rather than config, to support
    # MultiRecruiter experiments
    recruiter = recruiters.by_name(participant.recruiter_id)
    exp = Experiment(session)

    return recruiter.exit_response(experiment=exp, participant=participant)


@app.route("/summary", methods=["GET"])
def summary():
    """Summarize the participants' status codes."""
    exp = Experiment(session)
    state = {
        "status": "success",
        "summary": exp.log_summary(),
        "completed": exp.is_complete(),
    }
    unfilled_nets = (
        models.Network.query.filter(models.Network.full != true())
        .with_entities(models.Network.id, models.Network.max_size)
        .all()
    )
    working = (
        models.Participant.query.filter_by(status="working")
        .with_entities(func.count(models.Participant.id))
        .scalar()
    )
    state["unfilled_networks"] = len(unfilled_nets)
    nodes_remaining = 0
    required_nodes = 0
    if state["unfilled_networks"] == 0:
        if working == 0 and state["completed"] is None:
            state["completed"] = True
    else:
        for net in unfilled_nets:
            node_count = (
                models.Node.query.filter_by(network_id=net.id, failed=False)
                .with_entities(func.count(models.Node.id))
                .scalar()
            )
            net_size = net.max_size
            required_nodes += net_size
            nodes_remaining += net_size - node_count
    state["nodes_remaining"] = nodes_remaining
    state["required_nodes"] = required_nodes

    if state["completed"] is None:
        state["completed"] = False

    # Regenerate a waiting room message when checking status
    # to counter missed messages at the end of the waiting room
    nonfailed_count = models.Participant.query.filter(
        (models.Participant.status == "working")
        | (models.Participant.status == "overrecruited")
        | (models.Participant.status == "submitted")
        | (models.Participant.status == "approved")
    ).count()
    exp = Experiment(session)
    overrecruited = exp.is_overrecruited(nonfailed_count)
    if exp.quorum:
        quorum = {"q": exp.quorum, "n": nonfailed_count, "overrecruited": overrecruited}
        db.queue_message(WAITING_ROOM_CHANNEL, dumps(quorum))

    return Response(dumps(state), status=200, mimetype="application/json")


@app.route("/experiment_property/<prop>", methods=["GET"])
@app.route("/experiment/<prop>", methods=["GET"])
def experiment_property(prop):
    """Get a property of the experiment by name."""
    exp = Experiment(session)
    try:
        value = exp.public_properties[prop]
    except KeyError:
        abort(404)
    return success_response(**{prop: value})


@app.route("/<page>", methods=["GET"])
def get_page(page):
    """Return the requested page."""
    try:
        return render_template(page + ".html")
    except TemplateNotFound:
        abort(404)


@app.route("/<directory>/<page>", methods=["GET"])
def get_page_from_directory(directory, page):
    """Get a page from a given directory."""
    return render_template(directory + "/" + page + ".html")


@app.route("/consent")
def consent():
    """Return the consent form. Here for backwards-compatibility with 2.x."""
    config = _config()

    entry_information = request.args.to_dict()
    exp = Experiment(session)
    entry_data = exp.normalize_entry_information(entry_information)

    hit_id = entry_data.get("hit_id")
    assignment_id = entry_data.get("assignment_id")
    worker_id = entry_data.get("worker_id")
    return render_template(
        "consent.html",
        hit_id=hit_id,
        assignment_id=assignment_id,
        worker_id=worker_id,
        mode=config.get("mode"),
        query_string=request.query_string.decode(),
    )


"""Routes for reading and writing to the database."""


def request_parameter(parameter, parameter_type=None, default=None, optional=False):
    """Get a parameter from a request.

    parameter is the name of the parameter you are looking for
    parameter_type is the type the parameter should have
    default is the value the parameter takes if it has not been passed

    If the parameter is not found and no default is specified,
    or if the parameter is found but is of the wrong type
    then a Response object is returned
    """
    exp = Experiment(session)

    # get the parameter
    try:
        value = request.values[parameter]
    except KeyError:
        # if it isnt found use the default, or return an error Response
        if default is not None:
            return default
        elif optional:
            return None
        else:
            msg = "{} {} request, {} not specified".format(
                request.url, request.method, parameter
            )
            return error_response(error_type=msg)

    # check the parameter type
    if parameter_type is None:
        # if no parameter_type is required, return the parameter as is
        return value
    elif parameter_type == "int":
        # if int is required, convert to an int
        try:
            value = int(value)
            return value
        except ValueError:
            msg = "{} {} request, non-numeric {}: {}".format(
                request.url, request.method, parameter, value
            )
            return error_response(error_type=msg)
    elif parameter_type == "known_class":
        # if its a known class check against the known classes
        try:
            value = exp.known_classes[value]
            return value
        except KeyError:
            msg = "{} {} request, unknown_class: {} for parameter {}".format(
                request.url, request.method, value, parameter
            )
            return error_response(error_type=msg)
    elif parameter_type == "bool":
        # if its a boolean, convert to a boolean
        if value in ["True", "False"]:
            return value == "True"
        else:
            msg = "{} {} request, non-boolean {}: {}".format(
                request.url, request.method, parameter, value
            )
            return error_response(error_type=msg)
    else:
        msg = "/{} {} request, unknown parameter type: {} for parameter {}".format(
            request.url, request.method, parameter_type, parameter
        )
        return error_response(error_type=msg)


def assign_properties(thing):
    """Assign properties to an object.

    When creating something via a post request (e.g. a node), you can pass the
    properties of the object in the request. This function gets those values
    from the request and fills in the relevant columns of the table.
    """
    details = request_parameter(parameter="details", optional=True)
    if details:
        setattr(thing, "details", loads(details))

    for p in range(5):
        property_name = "property" + str(p + 1)
        property = request_parameter(parameter=property_name, optional=True)
        if property:
            setattr(thing, property_name, property)

    session.commit()


@app.route("/participant/<worker_id>/<hit_id>/<assignment_id>/<mode>", methods=["POST"])
@db.serialized
def create_participant(worker_id, hit_id, assignment_id, mode, entry_information=None):
    """Create a participant.

    This route is hit early on. Any nodes the participant creates will be
    defined in reference to the participant object. You must specify the
    worker_id, hit_id, assignment_id, and mode in the url.
    """
    # Lock the table, triggering multiple simultaneous accesses to fail
    try:
        session.connection().execute("LOCK TABLE participant IN EXCLUSIVE MODE NOWAIT")
    except exc.OperationalError as e:
        e.orig = TransactionRollbackError()
        raise e

    missing = [p for p in (worker_id, hit_id, assignment_id) if p == "undefined"]
    if missing:
        msg = "/participant POST: required values were 'undefined'"
        return error_response(error_type=msg, status=403)

    fingerprint_hash = request.args.get("fingerprint_hash")
    try:
        fingerprint_found = models.Participant.query.filter_by(
            fingerprint_hash=fingerprint_hash
        ).one_or_none()
    except MultipleResultsFound:
        fingerprint_found = True

    if fingerprint_hash and fingerprint_found:
        db.logger.warning("Same browser fingerprint detected.")

        if mode == "live":
            return error_response(
                error_type="/participant POST: Same participant dectected.", status=403
            )

    already_participated = models.Participant.query.filter_by(
        worker_id=worker_id
    ).one_or_none()

    if already_participated:
        db.logger.warning("Worker has already participated.")
        return error_response(
            error_type="/participant POST: worker has already participated.", status=403
        )

    duplicate = models.Participant.query.filter_by(
        assignment_id=assignment_id, status="working"
    ).one_or_none()

    if duplicate:
        msg = """
            AWS has reused assignment_id while existing participant is
            working. Replacing older participant {}.
        """
        app.logger.warning(msg.format(duplicate.id))
        q.enqueue(worker_function, "AssignmentReassigned", None, duplicate.id)

    # Count working or beyond participants.
    nonfailed_count = (
        models.Participant.query.filter(
            (models.Participant.status == "working")
            | (models.Participant.status == "overrecruited")
            | (models.Participant.status == "submitted")
            | (models.Participant.status == "approved")
        ).count()
        + 1
    )

    recruiter_name = request.args.get("recruiter")

    # Create the new participant.
    exp = Experiment(session)
    participant = exp.create_participant(
        worker_id,
        hit_id,
        assignment_id,
        mode,
        recruiter_name,
        fingerprint_hash,
        entry_information,
    )

    session.flush()
    overrecruited = exp.is_overrecruited(nonfailed_count)
    if overrecruited:
        participant.status = "overrecruited"

    result = {"participant": participant.__json__()}

    # Queue notification to others in waiting room
    if exp.quorum:
        quorum = {
            "q": exp.quorum,
            "n": nonfailed_count,
            "overrecruited": participant.status == "overrecruited",
        }
        db.queue_message(WAITING_ROOM_CHANNEL, dumps(quorum))
        result["quorum"] = quorum

    # return the data
    return success_response(**result)


@app.route("/participant", methods=["POST"])
def post_participant():
    config = _config()
    entry_information = request.form.to_dict()
    # Remove the mode from entry_information if provided
    mode = entry_information.pop("mode", config.get("mode"))
    exp = Experiment(session)
    participant_info = exp.normalize_entry_information(entry_information)
    return create_participant(mode=mode, **participant_info)


@app.route("/participant/<participant_id>", methods=["GET"])
def get_participant(participant_id):
    """Get the participant with the given id."""
    try:
        ppt = models.Participant.query.filter_by(id=participant_id).one()
    except NoResultFound:
        return error_response(
            error_type="/participant GET: no participant found", status=403
        )

    # return the data
    return success_response(participant=ppt.__json__())


@app.route("/load-participant", methods=["POST"])
def load_participant():
    """Get the participant with an assignment id provided in the request.
    Delegates to :func:`~dallinger.experiments.Experiment.load_participant`.
    """
    entry_information = request.form.to_dict()
    exp = Experiment(session)
    participant_info = exp.normalize_entry_information(entry_information)

    assignment_id = participant_info.get("assignment_id")
    if assignment_id is None:
        return error_response(
            error_type="/load-participant POST: no participant found", status=403
        )
    ppt = exp.load_participant(assignment_id)
    if ppt is None:
        return error_response(
            error_type="/load-participant POST: no participant found", status=403
        )

    # return the data
    return success_response(participant=ppt.__json__())


@app.route("/network/<network_id>", methods=["GET"])
def get_network(network_id):
    """Get the network with the given id."""
    try:
        net = models.Network.query.filter_by(id=network_id).one()
    except NoResultFound:
        return error_response(error_type="/network GET: no network found", status=403)

    # return the data
    return success_response(network=net.__json__())


@app.route("/question/<participant_id>", methods=["POST"])
def create_question(participant_id):
    """Send a POST request to the question table.

    Questions store information at the participant level, not the node
    level.
    You should pass the question (string) number (int) and response
    (string) as arguments.
    """
    # Get the participant.
    try:
        ppt = models.Participant.query.filter_by(id=participant_id).one()
    except NoResultFound:
        return error_response(
            error_type="/question POST no participant found", status=403
        )

    question = request_parameter(parameter="question")
    response = request_parameter(parameter="response")
    number = request_parameter(parameter="number", parameter_type="int")
    for x in [question, response, number]:
        if isinstance(x, Response):
            return x

    # Consult the recruiter regarding whether to accept a questionnaire
    # from the participant:
    rejection = ppt.recruiter.rejects_questionnaire_from(ppt)
    if rejection:
        return error_response(
            error_type="/question POST, status = {}, reason: {}".format(
                ppt.status, rejection
            ),
            participant=ppt,
        )

    config = get_config()
    question_max_length = config.get("question_max_length", 1000)

    if len(question) > question_max_length or len(response) > question_max_length:
        return error_response(error_type="/question POST length too long", status=400)

    try:
        # execute the request
        models.Question(
            participant=ppt, question=question, response=response, number=number
        )
        session.commit()
    except Exception:
        return error_response(error_type="/question POST server error", status=403)

    # return the data
    return success_response()


@app.route("/node/<int:node_id>/neighbors", methods=["GET"])
def node_neighbors(node_id):
    """Send a GET request to the node table.

    This calls the neighbours method of the node
    making the request and returns a list of descriptions of
    the nodes (even if there is only one).
    Required arguments: participant_id, node_id
    Optional arguments: type, connection

    After getting the neighbours it also calls
    exp.node_get_request()
    """
    exp = Experiment(session)

    # get the parameters
    node_type = request_parameter(
        parameter="node_type", parameter_type="known_class", default=models.Node
    )
    connection = request_parameter(parameter="connection", default="to")
    failed = request_parameter(parameter="failed", parameter_type="bool", optional=True)
    for x in [node_type, connection]:
        if type(x) == Response:
            return x

    # make sure the node exists
    node = models.Node.query.get(node_id)
    if node is None:
        return error_response(
            error_type="/node/neighbors, node does not exist",
            error_text="/node/{0}/neighbors, node {0} does not exist".format(node_id),
        )

    # get its neighbors
    if failed is not None:
        # This will always raise because "failed" is not a supported parameter.
        # We just want to pass the exception message back in the response:
        try:
            node.neighbors(type=node_type, direction=connection, failed=failed)
        except Exception as e:
            return error_response(error_type="node.neighbors", error_text=str(e))

    else:
        nodes = node.neighbors(type=node_type, direction=connection)
        try:
            # ping the experiment
            exp.node_get_request(node=node, nodes=nodes)
            session.commit()
        except Exception:
            return error_response(error_type="exp.node_get_request")

    return success_response(nodes=[n.__json__() for n in nodes])


@app.route("/node/<participant_id>", methods=["POST"])
@db.serialized
def create_node(participant_id):
    """Send a POST request to the node table.

    This makes a new node for the participant, it calls:
        1. exp.get_network_for_participant
        2. exp.create_node
        3. exp.add_node_to_network
        4. exp.node_post_request
    """
    exp = Experiment(session)

    # Get the participant.
    try:
        participant = models.Participant.query.filter_by(id=participant_id).one()
    except NoResultFound:
        return error_response(error_type="/node POST no participant found", status=403)

    # Make sure the participant status is working
    if participant.status != "working":
        error_type = "/node POST, status = {}".format(participant.status)
        return error_response(error_type=error_type, participant=participant)

    # execute the request
    network = exp.get_network_for_participant(participant=participant)
    if network is None:
        return Response(dumps({"status": "error"}), status=403)

    node = exp.create_node(participant=participant, network=network)
    assign_properties(node)
    exp.add_node_to_network(node=node, network=network)

    # ping the experiment
    exp.node_post_request(participant=participant, node=node)

    # return the data
    return success_response(node=node.__json__())


@app.route("/node/<int:node_id>/vectors", methods=["GET"])
def node_vectors(node_id):
    """Get the vectors of a node.

    You must specify the node id in the url.
    You can pass direction (incoming/outgoing/all) and failed
    (True/False/all).
    """
    exp = Experiment(session)
    # get the parameters
    direction = request_parameter(parameter="direction", default="all")
    failed = request_parameter(parameter="failed", parameter_type="bool", default=False)
    for x in [direction, failed]:
        if type(x) == Response:
            return x

    # execute the request
    node = models.Node.query.get(node_id)
    if node is None:
        return error_response(error_type="/node/vectors, node does not exist")

    try:
        vectors = node.vectors(direction=direction, failed=failed)
        exp.vector_get_request(node=node, vectors=vectors)
        session.commit()
    except Exception:
        return error_response(
            error_type="/node/vectors GET server error",
            status=403,
            participant=node.participant,
        )

    # return the data
    return success_response(vectors=[v.__json__() for v in vectors])


@app.route("/node/<int:node_id>/connect/<int:other_node_id>", methods=["POST"])
def connect(node_id, other_node_id):
    """Connect to another node.

    The ids of both nodes must be speficied in the url.
    You can also pass direction (to/from/both) as an argument.
    """
    exp = Experiment(session)

    # get the parameters
    direction = request_parameter(parameter="direction", default="to")
    if type(direction == Response):
        return direction

    # check the nodes exist
    node = models.Node.query.get(node_id)
    if node is None:
        return error_response(error_type="/node/connect, node does not exist")

    other_node = models.Node.query.get(other_node_id)
    if other_node is None:
        return error_response(
            error_type="/node/connect, other node does not exist",
            participant=node.participant,
        )

    # execute the request
    try:
        vectors = node.connect(whom=other_node, direction=direction)
        for v in vectors:
            assign_properties(v)

        # ping the experiment
        exp.vector_post_request(node=node, vectors=vectors)

        session.commit()
    except Exception:
        return error_response(
            error_type="/vector POST server error",
            status=403,
            participant=node.participant,
        )

    return success_response(vectors=[v.__json__() for v in vectors])


@app.route("/info/<int:node_id>/<int:info_id>", methods=["GET"])
def get_info(node_id, info_id):
    """Get a specific info.

    Both the node and info id must be specified in the url.
    """
    exp = Experiment(session)

    # check the node exists
    node = models.Node.query.get(node_id)
    if node is None:
        return error_response(error_type="/info, node does not exist")

    # execute the experiment method:
    info = models.Info.query.get(info_id)
    if info is None:
        return error_response(
            error_type="/info GET, info does not exist", participant=node.participant
        )
    elif info.origin_id != node.id and info.id not in [
        t.info_id for t in node.transmissions(direction="incoming", status="received")
    ]:
        return error_response(
            error_type="/info GET, forbidden info",
            status=403,
            participant=node.participant,
        )

    try:
        # ping the experiment
        exp.info_get_request(node=node, infos=info)
        session.commit()
    except Exception:
        return error_response(
            error_type="/info GET server error",
            status=403,
            participant=node.participant,
        )

    # return the data
    return success_response(info=info.__json__())


@app.route("/node/<int:node_id>/infos", methods=["GET"])
def node_infos(node_id):
    """Get all the infos of a node.

    The node id must be specified in the url.
    You can also pass info_type.
    """
    exp = Experiment(session)

    # get the parameters
    info_type = request_parameter(
        parameter="info_type", parameter_type="known_class", default=models.Info
    )
    if type(info_type) == Response:
        return info_type

    # check the node exists
    node = models.Node.query.get(node_id)
    if node is None:
        return error_response(error_type="/node/infos, node does not exist")

    try:
        # execute the request:
        infos = node.infos(type=info_type)

        # ping the experiment
        exp.info_get_request(node=node, infos=infos)

        session.commit()
    except Exception:
        return error_response(
            error_type="/node/infos GET server error",
            status=403,
            participant=node.participant,
        )

    return success_response(infos=[i.__json__() for i in infos])


@app.route("/node/<int:node_id>/received_infos", methods=["GET"])
def node_received_infos(node_id):
    """Get all the infos a node has been sent and has received.

    You must specify the node id in the url.
    You can also pass the info type.
    """
    exp = Experiment(session)

    # get the parameters
    info_type = request_parameter(
        parameter="info_type", parameter_type="known_class", default=models.Info
    )
    if type(info_type) == Response:
        return info_type

    # check the node exists
    node = models.Node.query.get(node_id)
    if node is None:
        return error_response(
            error_type="/node/infos, node {} does not exist".format(node_id)
        )

    # execute the request:
    infos = node.received_infos(type=info_type)

    try:
        # ping the experiment
        exp.info_get_request(node=node, infos=infos)

        session.commit()
    except Exception:
        return error_response(
            error_type="info_get_request error",
            status=403,
            participant=node.participant,
        )

    return success_response(infos=[i.__json__() for i in infos])


@app.route("/tracking_event/<int:node_id>", methods=["POST"])
@crossdomain(origin="*")
def tracking_event_post(node_id):
    """Enqueue a TrackingEvent worker for the specified Node."""
    details = request_parameter(parameter="details", optional=True)
    if details:
        details = loads(details)

    # check the node exists
    node = models.Node.query.get(node_id)
    if node is None:
        return error_response(error_type="/info POST, node does not exist")

    db.logger.debug(
        "rq: Queueing %s with for node: %s for worker_function",
        "TrackingEvent",
        node_id,
    )
    q.enqueue(
        worker_function, "TrackingEvent", None, None, node_id=node_id, details=details
    )

    return success_response(details=details)


@app.route("/info/<int:node_id>", methods=["POST"])
@crossdomain(origin="*")
def info_post(node_id):
    """Create an info.

    The node id must be specified in the url.

    You must pass contents as an argument.
    info_type is an additional optional argument.
    If info_type is a custom subclass of Info it must be
    added to the known_classes of the experiment class.
    """
    # get the parameters and validate them
    contents = request_parameter(parameter="contents")
    info_type = request_parameter(
        parameter="info_type", parameter_type="known_class", default=models.Info
    )
    failed = request_parameter(parameter="failed", parameter_type="bool", default=False)

    for x in [contents, info_type]:
        if type(x) == Response:
            return x
    # check the node exists
    node = models.Node.query.get(node_id)
    if node is None:
        return error_response(error_type="/info POST, node does not exist")

    exp = Experiment(session)
    try:
        # execute the request
        additional_params = {}
        if failed:
            additional_params["failed"] = failed
        info = info_type(origin=node, contents=contents, **additional_params)
        assign_properties(info)

        # ping the experiment
        exp.info_post_request(node=node, info=info)

        session.commit()
    except Exception:
        return error_response(
            error_type="/info POST server error",
            status=403,
            participant=node.participant,
        )

    # return the data
    return success_response(info=info.__json__())


@app.route("/node/<int:node_id>/transmissions", methods=["GET"])
def node_transmissions(node_id):
    """Get all the transmissions of a node.

    The node id must be specified in the url.
    You can also pass direction (to/from/all) or status (all/pending/received)
    as arguments.
    """
    exp = Experiment(session)

    # get the parameters
    direction = request_parameter(parameter="direction", default="incoming")
    status = request_parameter(parameter="status", default="all")
    for x in [direction, status]:
        if type(x) == Response:
            return x

    # check the node exists
    node = models.Node.query.get(node_id)
    if node is None:
        return error_response(error_type="/node/transmissions, node does not exist")

    # execute the request
    transmissions = node.transmissions(direction=direction, status=status)

    try:
        if direction in ["incoming", "all"] and status in ["pending", "all"]:
            node.receive()
            session.commit()
        # ping the experiment
        exp.transmission_get_request(node=node, transmissions=transmissions)
        session.commit()
    except Exception:
        return error_response(
            error_type="/node/transmissions GET server error",
            status=403,
            participant=node.participant,
        )

    # return the data
    return success_response(transmissions=[t.__json__() for t in transmissions])


@app.route("/node/<int:node_id>/transmit", methods=["POST"])
def node_transmit(node_id):
    """Transmit to another node.

    The sender's node id must be specified in the url.

    As with node.transmit() the key parameters are what and to_whom. However,
    the values these accept are more limited than for the back end due to the
    necessity of serialization.

    If what and to_whom are not specified they will default to None.
    Alternatively you can pass an int (e.g. '5') or a class name (e.g. 'Info' or
    'Agent'). Passing an int will get that info/node, passing a class name will
    pass the class. Note that if the class you are specifying is a custom class
    it will need to be added to the dictionary of known_classes in your
    experiment code.

    You may also pass the values property1, property2, property3, property4,
    property5 and details. If passed this will fill in the relevant values of
    the transmissions created with the values you specified.

    For example, to transmit all infos of type Meme to the node with id 10:
    dallinger.post(
        "/node/" + my_node_id + "/transmit",
        {what: "Meme",
         to_whom: 10}
    );
    """
    exp = Experiment(session)
    what = request_parameter(parameter="what", optional=True)
    to_whom = request_parameter(parameter="to_whom", optional=True)

    # check the node exists
    node = models.Node.query.get(node_id)
    if node is None:
        return error_response(error_type="/node/transmit, node does not exist")
    # create what
    if what is not None:
        try:
            what = int(what)
            what = models.Info.query.get(what)
            if what is None:
                return error_response(
                    error_type="/node/transmit POST, info does not exist",
                    participant=node.participant,
                )
        except Exception:
            try:
                what = exp.known_classes[what]
            except KeyError:
                msg = "/node/transmit POST, {} not in experiment.known_classes"
                return error_response(
                    error_type=msg.format(what), participant=node.participant
                )

    # create to_whom
    if to_whom is not None:
        try:
            to_whom = int(to_whom)
            to_whom = models.Node.query.get(to_whom)
            if to_whom is None:
                return error_response(
                    error_type="/node/transmit POST, recipient Node does not exist",
                    participant=node.participant,
                )
        except Exception:
            try:
                to_whom = exp.known_classes[to_whom]
            except KeyError:
                msg = "/node/transmit POST, {} not in experiment.known_classes"
                return error_response(
                    error_type=msg.format(to_whom), participant=node.participant
                )

    # execute the request
    try:
        transmissions = node.transmit(what=what, to_whom=to_whom)
        for t in transmissions:
            assign_properties(t)
        session.commit()
        # ping the experiment
        exp.transmission_post_request(node=node, transmissions=transmissions)
        session.commit()
    except Exception:
        return error_response(
            error_type="/node/transmit POST, server error", participant=node.participant
        )

    # return the data
    return success_response(transmissions=[t.__json__() for t in transmissions])


@app.route("/node/<int:node_id>/transformations", methods=["GET"])
def transformation_get(node_id):
    """Get all the transformations of a node.

    The node id must be specified in the url.

    You can also pass transformation_type.
    """
    exp = Experiment(session)

    # get the parameters
    transformation_type = request_parameter(
        parameter="transformation_type",
        parameter_type="known_class",
        default=models.Transformation,
    )
    if type(transformation_type) == Response:
        return transformation_type

    # check the node exists
    node = models.Node.query.get(node_id)
    if node is None:
        return error_response(
            error_type="/node/transformations, "
            "node {} does not exist".format(node_id)
        )

    # execute the request
    transformations = node.transformations(type=transformation_type)
    try:
        # ping the experiment
        exp.transformation_get_request(node=node, transformations=transformations)
        session.commit()
    except Exception:
        return error_response(
            error_type="/node/transformations GET failed", participant=node.participant
        )

    # return the data
    return success_response(transformations=[t.__json__() for t in transformations])


@app.route(
    "/transformation/<int:node_id>/<int:info_in_id>/<int:info_out_id>", methods=["POST"]
)
def transformation_post(node_id, info_in_id, info_out_id):
    """Transform an info.

    The ids of the node, info in and info out must all be in the url.
    You can also pass transformation_type.
    """
    exp = Experiment(session)

    # Get the parameters.
    transformation_type = request_parameter(
        parameter="transformation_type",
        parameter_type="known_class",
        default=models.Transformation,
    )
    if type(transformation_type) == Response:
        return transformation_type

    # Check that the node etc. exists.
    node = models.Node.query.get(node_id)
    if node is None:
        return error_response(
            error_type="/transformation POST, " "node {} does not exist".format(node_id)
        )

    info_in = models.Info.query.get(info_in_id)
    if info_in is None:
        return error_response(
            error_type="/transformation POST, info_in {} does not exist".format(
                info_in_id
            ),
            participant=node.participant,
        )

    info_out = models.Info.query.get(info_out_id)
    if info_out is None:
        return error_response(
            error_type="/transformation POST, info_out {} does not exist".format(
                info_out_id
            ),
            participant=node.participant,
        )

    try:
        # execute the request
        transformation = transformation_type(info_in=info_in, info_out=info_out)
        assign_properties(transformation)
        session.commit()

        # ping the experiment
        exp.transformation_post_request(node=node, transformation=transformation)
        session.commit()
    except Exception:
        return error_response(
            error_type="/transformation POST failed", participant=node.participant
        )

    # return the data
    return success_response(transformation=transformation.__json__())


@app.route("/notifications", methods=["POST", "GET"])
@crossdomain(origin="*")
def api_notifications():
    """Receive MTurk REST notifications."""
    event_type = request.values["Event.1.EventType"]
    assignment_id = request.values.get("Event.1.AssignmentId")
    participant_id = request.values.get("participant_id")

    # Add the notification to the queue.
    db.logger.debug(
        "rq: Queueing %s with id: %s for worker_function", event_type, assignment_id
    )
    q.enqueue(worker_function, event_type, assignment_id, participant_id)
    db.logger.debug("rq: Submitted Queue Length: %d (%s)", len(q), ", ".join(q.job_ids))

    return success_response()


def check_for_duplicate_assignments(participant):
    """Check that the assignment_id of the participant is unique.

    If it isnt the older participants will be failed.
    """
    participants = models.Participant.query.filter_by(
        assignment_id=participant.assignment_id
    ).all()
    duplicates = [
        p for p in participants if (p.id != participant.id and p.status == "working")
    ]
    for d in duplicates:
        q.enqueue(worker_function, "AssignmentAbandoned", None, d.id)


@app.route("/worker_complete", methods=["POST"])
@db.scoped_session_decorator
def worker_complete():
    """Complete worker."""
    participant_id = request.values.get("participant_id")
    if not participant_id:
        return error_response(
            error_type="bad request", error_text="participantId parameter is required"
        )

    try:
        _worker_complete(participant_id)
    except KeyError:
        return error_response(
            error_type="ParticipantId not found: {}".format(participant_id)
        )

    return success_response(status="success")


def _worker_complete(participant_id):
    participants = models.Participant.query.filter_by(id=participant_id).all()
    if not participants:
        raise KeyError()

    participant = participants[0]

    participant.end_time = datetime.now()
    session.add(participant)
    session.commit()

    # Notify experiment that participant has been marked complete
    exp = Experiment(session)
    exp.participant_task_completed(participant)

    event_type = participant.recruiter.submitted_event()

    if event_type is None:
        return

    worker_function(
        event_type=event_type,
        assignment_id=participant.assignment_id,
        participant_id=participant_id,
    )


@app.route("/worker_failed", methods=["GET"])
@db.scoped_session_decorator
def worker_failed():
    """Fail worker. Used by bots only for now."""
    participant_id = request.args.get("participant_id")
    if not participant_id:
        return error_response(
            error_type="bad request", error_text="participantId parameter is required"
        )

    try:
        _worker_failed(participant_id)
    except KeyError:
        return error_response(
            error_type="ParticipantId not found: {}".format(participant_id)
        )

    return success_response(
        field="status", data="success", request_type="worker failed"
    )


def _worker_failed(participant_id):
    participants = models.Participant.query.filter_by(id=participant_id).all()
    if not participants:
        raise KeyError()

    participant = participants[0]
    participant.end_time = datetime.now()
    session.add(participant)
    session.commit()
    # TODO: Recruiter.rejected_event/failed_event (replace conditional w/ polymorphism)
    if participant.recruiter_id == "bots" or participant.recruiter_id.startswith(
        "bots:"
    ):
        worker_function(
            assignment_id=participant.assignment_id,
            participant_id=participant.id,
            event_type="BotAssignmentRejected",
        )


# Insert "mode" into pages so it's carried from page to page done server-side
# to avoid breaking backwards compatibility with old templates.
def insert_mode(page_html, mode):
    """Insert mode."""
    match_found = False
    matches = re.finditer("workerId={{ workerid }}", page_html)
    match = None
    for match in matches:
        match_found = True
    if match_found:
        new_html = page_html[: match.end()] + "&mode=" + mode + page_html[match.end() :]
        return new_html
    else:
        raise ExperimentError("insert_mode_failed")
