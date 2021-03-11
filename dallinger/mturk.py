import boto3
import datetime
import logging
import time

from botocore.exceptions import ClientError
from botocore.exceptions import NoCredentialsError
from cached_property import cached_property


logger = logging.getLogger(__file__)
PERCENTAGE_APPROVED_REQUIREMENT_ID = "000000000000000000L0"
LOCALE_REQUIREMENT_ID = "00000000000000000071"
MAX_SUPPORTED_BATCH_SIZE = 100


class MTurkServiceException(Exception):
    """Custom exception type"""


class RemoteAPICallTimedOut(MTurkServiceException):
    """A call to a remote API took too long."""


class DuplicateQualificationNameError(MTurkServiceException):
    """A Qualification with the given name already exists."""


class QualificationNotFoundException(MTurkServiceException):
    """A Qualification searched for by name does not exist."""


class WorkerLacksQualification(MTurkServiceException):
    """The worker has not been granted a given Qualification."""


class RevokedQualification(MTurkServiceException):
    """The Qualification has been revoked for this worker."""


class NonExistentSubscription(MTurkServiceException):
    """The SNS subscription does not exist."""


class SNSService(object):
    """Handles AWS SNS subscriptions"""

    max_wait_secs = 12

    def __init__(
        self, aws_access_key_id, aws_secret_access_key, region_name, confirm=True
    ):
        self.aws_key = aws_access_key_id
        self.aws_secret = aws_secret_access_key
        self.region_name = region_name
        self.do_confirm_subscription = confirm

    @cached_property
    def _sns(self):
        session = boto3.session.Session(
            aws_access_key_id=self.aws_key,
            aws_secret_access_key=self.aws_secret,
            region_name=self.region_name,
        )
        return session.client("sns")

    def confirm_subscription(self, token, topic):
        logger.warning("Confirming SNS subsription.")
        self._sns.confirm_subscription(
            # AuthenticateOnUnsubscribe=True,  # Should we care?
            Token=token,
            TopicArn=topic,
        )

    def create_subscription(self, experiment_id, notification_url):
        logger.warning(
            "Creating new SNS subscription for {}...".format(notification_url)
        )
        protocol = "https" if notification_url.startswith("https") else "http"
        topic = self._sns.create_topic(Name=experiment_id)
        subscription = self._sns.subscribe(
            TopicArn=topic["TopicArn"],
            Protocol=protocol,
            Endpoint=notification_url,
            ReturnSubscriptionArn=True,  # So we can start polling the status
        )

        start = time.time()
        while self._awaiting_confirmation(subscription):
            elapsed = time.time() - start
            if elapsed > self.max_wait_secs:
                raise RemoteAPICallTimedOut("Too long")
            logger.warning("Awaiting SNS subscription confirmation...")
            time.sleep(1)

        logger.warning("Subscription confirmed.")
        return topic["TopicArn"]

    def cancel_subscription(self, experiment_id):
        logger.warning("Cancelling SNS subscription")
        topic_id = self._get_sns_topic_for_experiment(experiment_id)
        if topic_id is None:
            raise NonExistentSubscription(
                "No SNS subscription found for {}".format(experiment_id)
            )
        self._sns.delete_topic(TopicArn=topic_id)
        return True

    def _awaiting_confirmation(self, subscription):
        if not self.do_confirm_subscription:
            return False

        report = self._sns.get_subscription_attributes(
            SubscriptionArn=subscription["SubscriptionArn"]
        )
        status = report["Attributes"]["PendingConfirmation"]
        return status == "true"

    def _get_sns_topic_for_experiment(self, experiment_id):
        experiment_topics = (
            t for t in self._all_topics() if t.endswith(":" + experiment_id)
        )
        try:
            return next(experiment_topics)
        except StopIteration:
            return None

    def _all_topics(self):
        done = False
        next_token = None
        while not done:
            if next_token is not None:
                response = self._sns.list_topics(NextToken=next_token)
            else:
                response = self._sns.list_topics()

            if response:
                for t in response["Topics"]:
                    yield t["TopicArn"]
            if "NextToken" in response:
                next_token = response["NextToken"]
            else:
                done = True


class MTurkQuestions(object):
    """Creates MTurk HIT Question definitions:
    https://docs.aws.amazon.com/AWSMechTurk/latest/AWSMturkAPI/ApiReference_QuestionAnswerDataArticle.html
    """

    @staticmethod
    def external(ad_url, frame_height=600):
        q = (
            '<ExternalQuestion xmlns="http://mechanicalturk.amazonaws.com/'
            'AWSMechanicalTurkDataSchemas/2006-07-14/ExternalQuestion.xsd">'
            "<ExternalURL>{}</ExternalURL>"
            "<FrameHeight>{}</FrameHeight></ExternalQuestion>"
        )
        return q.format(ad_url, frame_height)

    @staticmethod
    def compensation(title="Compensation HIT", sandbox=False, frame_height=600):
        if sandbox:
            action = "https://workersandbox.mturk.com/mturk/externalSubmit"
        else:
            action = "https://www.mturk.com/mturk/externalSubmit"

        q = (
            '<HTMLQuestion xmlns="http://mechanicalturk.amazonaws.com/AWSMechanicalTurkDataSchemas/2011-11-11/HTMLQuestion.xsd">'
            "<HTMLContent><![CDATA[<!DOCTYPE html><html>"
            "<head>"
            '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8"/>'
            '<script type="text/javascript" src="https://s3.amazonaws.com/mturk-public/externalHIT_v1.js"></script>'
            "</head>"
            "<body>"
            '<form name="mturk_form" method="post" id="mturk_form" action="{}">'
            '<input type="hidden" value="" name="assignmentId" id="assignmentId"/>'
            "<h1>{}</h1>"
            "<p>We are sorry that you encountered difficulties with our experiment. "
            "We will compensate you immediately upon submission of this HIT.</p>"
            '<input type="hidden" name="some-input-required" value="anything" ></input>'
            '<input type="submit" id="submitButton" value="Submit" /></p></form>'
            '<script language="Javascript">turkSetAssignmentID();</script>'
            "</body></html>]]>"
            "</HTMLContent>"
            "<FrameHeight>{}</FrameHeight>"
            "</HTMLQuestion>"
        )

        return q.format(action, title, frame_height)


class MTurkQualificationRequirements(object):
    """Syntactic correctness for MTurk QualificationRequirements"""

    @staticmethod
    def min_approval(percentage):
        return {
            "QualificationTypeId": PERCENTAGE_APPROVED_REQUIREMENT_ID,
            "Comparator": "GreaterThanOrEqualTo",
            "IntegerValues": [percentage],
            "RequiredToPreview": True,
        }

    @staticmethod
    def restrict_to_countries(countries):
        return {
            "QualificationTypeId": LOCALE_REQUIREMENT_ID,
            "Comparator": "EqualTo",
            "LocaleValues": [{"Country": country} for country in countries],
            "RequiredToPreview": True,
        }

    @staticmethod
    def must_have(qualification_id):
        return {
            "QualificationTypeId": qualification_id,
            "Comparator": "Exists",
            "RequiredToPreview": True,
        }

    @staticmethod
    def must_not_have(qualification_id):
        return {
            "QualificationTypeId": qualification_id,
            "Comparator": "DoesNotExist",
            "RequiredToPreview": True,
        }


class MTurkService(object):
    """Facade for Amazon Mechanical Turk services provided via the boto3
    library.
    """

    def __init__(
        self,
        aws_access_key_id,
        aws_secret_access_key,
        region_name,
        sandbox=True,
        max_wait_secs=0,
    ):
        self.aws_key = aws_access_key_id
        self.aws_secret = aws_secret_access_key
        self.region_name = region_name
        self.is_sandbox = sandbox
        self.max_wait_secs = max_wait_secs

    @cached_property
    def mturk(self):
        session = boto3.session.Session(
            aws_access_key_id=self.aws_key,
            aws_secret_access_key=self.aws_secret,
            region_name=self.region_name,
        )
        return session.client(
            "mturk", endpoint_url=self.host, region_name=self.region_name
        )

    @cached_property
    def sns(self):
        return SNSService(
            aws_access_key_id=self.aws_key,
            aws_secret_access_key=self.aws_secret,
            region_name=self.region_name,
        )

    @property
    def host(self):
        if self.is_sandbox:
            template = u"https://mturk-requester-sandbox.{}.amazonaws.com"
        else:
            template = u"https://mturk-requester.{}.amazonaws.com"
        return template.format(self.region_name)

    def account_balance(self):
        response = self.mturk.get_account_balance()
        return float(response["AvailableBalance"])

    def check_credentials(self):
        """Verifies key/secret/host combination by making a balance inquiry"""
        try:
            return bool(self.mturk.get_account_balance())
        except NoCredentialsError:
            raise MTurkServiceException("No AWS credentials set!")
        except ClientError:
            raise MTurkServiceException("Invalid AWS credentials!")
        except Exception as ex:
            raise MTurkServiceException(
                "Error checking credentials: {}".format(str(ex))
            )

    def confirm_subscription(self, token, topic):
        """Called by the MTurkRecruiter Flask route"""
        self.sns.confirm_subscription(token=token, topic=topic)

    def create_qualification_type(self, name, description, status="Active"):
        """Create a new qualification Workers can be scored for."""
        try:
            response = self.mturk.create_qualification_type(
                Name=name, Description=description, QualificationTypeStatus=status
            )
        except Exception as ex:
            if "already created a QualificationType with this name" in str(ex):
                raise DuplicateQualificationNameError(str(ex))
            else:
                raise

        return self._translate_qtype(response["QualificationType"])

    def get_qualification_type_by_name(self, name):
        """Return a Qualification Type by name. If the provided name matches
        more than one Qualification, check to see if any of the results
        match the provided name exactly. If there's an exact match, return
        that Qualification. Otherwise, raise an exception.
        """
        max_fuzzy_matches_to_check = 100
        query = name.upper()
        args = {
            "Query": query,
            "MustBeRequestable": False,
            "MustBeOwnedByCaller": True,
            "MaxResults": max_fuzzy_matches_to_check,
        }
        results = self.mturk.list_qualification_types(**args)["QualificationTypes"]
        # This loop is largely for tests, because there's some indexing that
        # needs to happen on MTurk for search to work:
        start = time.time()
        while not results:
            elapsed = time.time() - start
            if elapsed > self.max_wait_secs:
                return None
            time.sleep(1)
            results = self.mturk.list_qualification_types(**args)["QualificationTypes"]

        qualifications = [self._translate_qtype(r) for r in results]
        if len(qualifications) > 1:
            for qualification in qualifications:
                if qualification["name"].upper() == query:
                    return qualification

            raise MTurkServiceException("{} was not a unique name".format(query))

        return qualifications[0]

    def assign_qualification(self, qualification_id, worker_id, score, notify=False):
        """Score a worker for a specific qualification"""
        return self._is_ok(
            self.mturk.associate_qualification_with_worker(
                QualificationTypeId=qualification_id,
                WorkerId=worker_id,
                IntegerValue=score,
                SendNotification=notify,
            )
        )

    def increment_qualification_score(self, qualification_id, worker_id, notify=False):
        """Increment the current qualification score for a worker, on a
        qualification with the provided ID.
        """
        try:
            current_score = self.current_qualification_score(
                qualification_id, worker_id
            )
        except (WorkerLacksQualification, RevokedQualification):
            current_score = 0
        new_score = current_score + 1
        self.assign_qualification(qualification_id, worker_id, new_score, notify)

        return {"qtype": qualification_id, "score": new_score}

    def increment_named_qualification_score(self, name, worker_id, notify=False):
        """Increment the current qualification score for a worker, on a
        qualification with the provided name.
        """
        result = self.current_named_qualification_score(name, worker_id)
        current_score = result["score"] or 0
        new_score = current_score + 1
        qtype_id = result["qtype"]["id"]
        self.assign_qualification(qtype_id, worker_id, new_score, notify)

        return {"qtype": result["qtype"], "score": new_score}

    def revoke_qualification(self, qualification_id, worker_id, reason=""):
        return self._is_ok(
            self.mturk.disassociate_qualification_from_worker(
                QualificationTypeId=qualification_id, WorkerId=worker_id, Reason=reason
            )
        )

    def current_qualification_score(self, qualification_id, worker_id):
        """Return a worker's qualification score as an iteger."""
        try:
            response = self.mturk.get_qualification_score(
                QualificationTypeId=qualification_id, WorkerId=worker_id
            )
        except ClientError as ex:
            error = str(ex)
            if "does not exist" in error:
                raise WorkerLacksQualification(
                    "Worker {} does not have qualification {}.".format(
                        worker_id, qualification_id
                    )
                )
            if "operation can be called with a status of: Granted" in error:
                raise RevokedQualification(
                    "Worker {} has had qualification {} revoked.".format(
                        worker_id, qualification_id
                    )
                )

            raise MTurkServiceException(error)
        return response["Qualification"]["IntegerValue"]

    def current_named_qualification_score(self, name, worker_id):
        """Return the current score for a worker, on a qualification with the
        provided name.
        """
        qtype = self.get_qualification_type_by_name(name)
        if qtype is None:
            raise QualificationNotFoundException(
                'No Qualification exists with name "{}"'.format(name)
            )
        try:
            score = self.current_qualification_score(qtype["id"], worker_id)
        except (WorkerLacksQualification, RevokedQualification):
            score = None

        return {"qtype": qtype, "score": score}

    def dispose_qualification_type(self, qualification_id):
        """Remove a qualification type we created"""
        return self._is_ok(
            self.mturk.delete_qualification_type(QualificationTypeId=qualification_id)
        )

    def get_workers_with_qualification(self, qualification_id):
        """Get workers with the given qualification."""
        done = False
        next_token = None
        while not done:
            if next_token is not None:
                response = self.mturk.list_workers_with_qualification_type(
                    QualificationTypeId=qualification_id,
                    MaxResults=MAX_SUPPORTED_BATCH_SIZE,
                    Status="Granted",
                    NextToken=next_token,
                )
            else:
                response = self.mturk.list_workers_with_qualification_type(
                    QualificationTypeId=qualification_id,
                    MaxResults=MAX_SUPPORTED_BATCH_SIZE,
                    Status="Granted",
                )
            if response:
                for r in response["Qualifications"]:
                    yield {"id": r["WorkerId"], "score": r["IntegerValue"]}
            if "NextToken" in response:
                next_token = response["NextToken"]
            else:
                done = True

    def create_hit(
        self,
        experiment_id,
        title,
        description,
        keywords,
        reward,
        duration_hours,
        lifetime_days,
        question,
        max_assignments,
        notification_url=None,
        annotation=None,
        qualifications=(),
        do_subscribe=True,
    ):
        """Create the actual HIT and return a dict with its useful properties."""

        # We need a HIT_Type in order to register for notifications
        hit_type_id = self._register_hit_type(
            title, description, reward, duration_hours, keywords, qualifications
        )
        if do_subscribe:
            self._create_notification_subscription(
                experiment_id, notification_url, hit_type_id
            )
        params = {
            "HITTypeId": hit_type_id,
            "Question": question,
            "LifetimeInSeconds": int(
                datetime.timedelta(days=lifetime_days).total_seconds()
            ),
            "MaxAssignments": max_assignments,
            "UniqueRequestToken": self._request_token(),
        }
        if annotation:
            params["RequesterAnnotation"] = annotation

        response = self.mturk.create_hit_with_hit_type(**params)
        if "HIT" not in response:
            raise MTurkServiceException("HIT request was invalid for unknown reason.")
        return self._translate_hit(response["HIT"])

    def extend_hit(self, hit_id, number, duration_hours=None):
        """Extend an existing HIT and return an updated description"""
        self.create_additional_assignments_for_hit(hit_id, number)

        if duration_hours is not None:
            self.update_expiration_for_hit(hit_id, duration_hours)

        return self.get_hit(hit_id)

    def create_additional_assignments_for_hit(self, hit_id, number):
        try:
            response = self.mturk.create_additional_assignments_for_hit(
                HITId=hit_id,
                NumberOfAdditionalAssignments=number,
                UniqueRequestToken=self._request_token(),
            )
        except Exception as ex:
            raise MTurkServiceException(
                "Error: failed to add {} assignments to HIT: {}".format(number, str(ex))
            )
        return self._is_ok(response)

    def update_expiration_for_hit(self, hit_id, hours):
        hit = self.get_hit(hit_id)
        expiration = datetime.timedelta(hours=hours) + hit["expiration"]
        try:
            response = self.mturk.update_expiration_for_hit(
                HITId=hit_id, ExpireAt=expiration
            )
        except Exception as ex:
            raise MTurkServiceException(
                "Failed to extend time until expiration of HIT: {}\n{}".format(
                    expiration, str(ex)
                )
            )
        return self._is_ok(response)

    def disable_hit(self, hit_id, experiment_id):
        self.expire_hit(hit_id)
        try:
            self.sns.cancel_subscription(experiment_id)
        except NonExistentSubscription:
            pass

        try:
            result = self.mturk.delete_hit(HITId=hit_id)
        except Exception as ex:
            if "currently in the state 'Disposed'" in str(ex):
                # this means "already deleted"...
                return True
            raise

        return self._is_ok(result)

    def expire_hit(self, hit_id):
        """Expire a HIT, which will change its status to "Reviewable",
        allowing it to be deleted.
        """
        try:
            self.mturk.update_expiration_for_hit(HITId=hit_id, ExpireAt=0)
        except Exception as ex:
            raise MTurkServiceException(
                "Failed to expire HIT {}: {}".format(hit_id, str(ex))
            )
        return True

    def get_hit(self, hit_id):
        return self._translate_hit(self.mturk.get_hit(HITId=hit_id)["HIT"])

    def get_hits(self, hit_filter=lambda x: True):
        done = False
        next_token = None
        while not done:
            if next_token is not None:
                response = self.mturk.list_hits(
                    MaxResults=MAX_SUPPORTED_BATCH_SIZE, NextToken=next_token
                )
            else:
                response = self.mturk.list_hits(MaxResults=MAX_SUPPORTED_BATCH_SIZE)
            for hit in response["HITs"]:
                translated = self._translate_hit(hit)
                if hit_filter(translated):
                    yield translated
            if "NextToken" in response:
                next_token = response["NextToken"]
            else:
                done = True

    def grant_bonus(self, assignment_id, amount, reason):
        """Grant a bonus to the MTurk Worker.
        Issues a payment of money from your account to a Worker.  To
        be eligible for a bonus, the Worker must have submitted
        results for one of your HITs, and have had those results
        approved or rejected. This payment happens separately from the
        reward you pay to the Worker when you approve the Worker's
        assignment.
        """
        assignment = self.get_assignment(assignment_id)
        worker_id = assignment["worker_id"]
        amount_str = "{:.2f}".format(amount)
        try:
            return self._is_ok(
                self.mturk.send_bonus(
                    WorkerId=worker_id,
                    BonusAmount=amount_str,
                    AssignmentId=assignment_id,
                    Reason=reason,
                    UniqueRequestToken=self._request_token(),
                )
            )
        except ClientError as ex:
            error = "Failed to pay assignment {} bonus of {}: {}".format(
                assignment_id, amount_str, str(ex)
            )
            raise MTurkServiceException(error)

    def get_assignment(self, assignment_id):
        """Get an assignment by ID and reformat the response."""
        try:
            response = self.mturk.get_assignment(AssignmentId=assignment_id)
        except ClientError as ex:
            if "does not exist" in str(ex):
                return None
            raise
        return self._translate_assignment(response["Assignment"])

    def approve_assignment(self, assignment_id):
        """Approving an assignment initiates two payments from the
        Requester's Amazon.com account:
            1. The Worker who submitted the results is paid
               the reward specified in the HIT.
            2. Amazon Mechanical Turk fees are debited.
        """
        try:
            return self._is_ok(
                self.mturk.approve_assignment(AssignmentId=assignment_id)
            )
        except ClientError as ex:
            assignment = self.get_assignment(assignment_id)
            raise MTurkServiceException(
                "Failed to approve assignment {}, {}: {}".format(
                    assignment_id, str(assignment), str(ex)
                )
            )

    def _create_notification_subscription(
        self, experiment_id, notification_url, hit_type_id
    ):
        topic_arn = self.sns.create_subscription(experiment_id, notification_url)
        logger.warning("Updating HIT with confirmed subscription: {}".format(topic_arn))
        self.mturk.update_notification_settings(
            HITTypeId=hit_type_id,
            Notification={
                "Destination": topic_arn,
                "Transport": "SNS",
                "Version": "2014-08-15",
                "EventTypes": [
                    "AssignmentAccepted",
                    "AssignmentAbandoned",
                    "AssignmentReturned",
                    "AssignmentSubmitted",
                    "HITReviewable",
                    "HITExpired",
                ],
            },
            Active=True,
        )

    def _register_hit_type(
        self, title, description, reward, duration_hours, keywords, qualifications
    ):
        """Register HIT Type for this HIT and return the type's ID, which
        is required for creating a HIT.
        """
        reward = str(reward)
        duration_secs = int(datetime.timedelta(hours=duration_hours).total_seconds())
        hit_type = self.mturk.create_hit_type(
            Title=title,
            Description=description,
            Reward=reward,
            AssignmentDurationInSeconds=duration_secs,
            Keywords=",".join(keywords),
            AutoApprovalDelayInSeconds=0,
            QualificationRequirements=qualifications,
        )

        return hit_type["HITTypeId"]

    def _request_token(self):
        return str(time.time())

    def _translate_assignment(self, assignment):
        # Returns only a subset of included values since we don't use most
        # currently.
        translated = {
            "id": assignment["AssignmentId"],
            "status": assignment["AssignmentStatus"],
            "hit_id": assignment["HITId"],
            "worker_id": assignment["WorkerId"],
        }

        return translated

    def _translate_hit(self, hit):
        if "Keywords" in hit:
            keywords = [w.strip() for w in hit["Keywords"].split(",") if w.strip()]
        else:
            keywords = []
        translated = {
            "id": hit["HITId"],
            "type_id": hit["HITTypeId"],
            "created": hit["CreationTime"],
            "expiration": hit["Expiration"],
            "max_assignments": hit["MaxAssignments"],
            "title": hit["Title"],
            "description": hit["Description"],
            "keywords": keywords,
            "qualification_type_ids": [
                q["QualificationTypeId"] for q in hit["QualificationRequirements"]
            ],
            "reward": float(hit["Reward"]),
            "review_status": hit["HITReviewStatus"],
            "status": hit["HITStatus"],
            "annotation": hit.get("RequesterAnnotation"),
            "worker_url": self._worker_hit_url(hit["HITTypeId"]),
            "assignments_available": hit["NumberOfAssignmentsAvailable"],
            "assignments_completed": hit["NumberOfAssignmentsCompleted"],
            "assignments_pending": hit["NumberOfAssignmentsPending"],
        }

        return translated

    def _worker_hit_url(self, type_id):
        if self.is_sandbox:
            url = "https://workersandbox.mturk.com/projects/{}/tasks"
        else:
            url = "https://worker.mturk.com/projects/{}/tasks"

        return url.format(type_id)

    def _translate_qtype(self, qtype):
        return {
            "id": qtype["QualificationTypeId"],
            "created": qtype["CreationTime"],
            "name": qtype["Name"],
            "description": qtype["Description"],
            "status": qtype["QualificationTypeStatus"],
        }

    def _is_ok(self, response):
        return response == {} or list(response.keys()) == ["ResponseMetadata"]
