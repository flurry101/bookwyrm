"""Import a user from another Bookwyrm instance"""

import json
import logging
import math

from django.db.models import (
    ForeignKey,
    FileField,
    JSONField,
    TextChoices,
    CASCADE,
    PROTECT,
    SET_NULL,
)
from django.db.utils import IntegrityError
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _
from django.contrib.postgres.fields import ArrayField as DjangoArrayField

from bookwyrm import activitypub
from bookwyrm import models
from bookwyrm.tasks import app, IMPORTS
from bookwyrm.models.job import ParentJob, ChildJob, ParentTask, SubTask
from bookwyrm.utils.tar import BookwyrmTarFile

logger = logging.getLogger(__name__)


class BookwyrmImportJob(ParentJob):
    """entry for a specific request for importing a bookwyrm user backup"""

    archive_file = FileField(null=True, blank=True)
    import_data = JSONField(null=True)
    required = DjangoArrayField(
        models.fields.CharField(max_length=50, blank=True), blank=True
    )

    def start_job(self):
        """Start the job"""
        start_import_task.delay(job_id=self.id)

    @property
    def book_tasks(self):
        """How many import book tasks are there?"""
        return UserImportBook.objects.filter(parent_job=self).all()

    @property
    def status_tasks(self):
        """How many import status tasks are there?"""
        return UserImportPost.objects.filter(parent_job=self).all()

    @property
    def relationship_tasks(self):
        """How many import relationship tasks are there?"""
        return UserRelationshipImport.objects.filter(parent_job=self).all()

    @property
    def item_count(self):
        """How many total tasks are there?"""
        return self.book_tasks.count() + self.status_tasks.count()

    @property
    def pending_item_count(self):
        """How many tasks are incomplete?"""
        status = BookwyrmImportJob.Status
        book_tasks = self.book_tasks.filter(
            status__in=[status.PENDING, status.ACTIVE]
        ).count()

        status_tasks = self.status_tasks.filter(
            status__in=[status.PENDING, status.ACTIVE]
        ).count()

        relationship_tasks = self.relationship_tasks.filter(
            status__in=[status.PENDING, status.ACTIVE]
        ).count()

        return book_tasks + status_tasks + relationship_tasks

    @property
    def percent_complete(self):
        """How far along?"""
        item_count = self.item_count
        if not item_count:
            return 0
        return math.floor((item_count - self.pending_item_count) / item_count * 100)


class UserImportBook(ChildJob):
    """ChildJob to import each book.
    Equivalent to ImportItem when importing a csv file of books"""

    book = ForeignKey(models.Book, on_delete=SET_NULL, null=True, blank=True)
    book_data = JSONField(null=False)

    def start_job(self):
        """Start the job"""
        import_book_task.delay(child_id=self.id)


class UserImportPost(ChildJob):
    """ChildJob for comments, quotes, and reviews"""

    class StatusType(TextChoices):
        """Possible status types."""

        COMMENT = "comment", _("Comment")
        REVIEW = "review", _("Review")
        QUOTE = "quote", _("Quotation")

    json = JSONField(null=False)
    book = models.fields.ForeignKey(
        "Edition", on_delete=PROTECT, activitypub_field="inReplyToBook"
    )
    status_type = models.fields.CharField(
        max_length=10, choices=StatusType.choices, default=StatusType.COMMENT, null=True
    )

    def start_job(self):
        """Start the job"""
        upsert_statuses_task.delay(child_id=self.id)


class UserRelationshipImport(ChildJob):
    """ChildJob for follows and blocks"""

    class RelationshipType(TextChoices):
        """Possible relationship types."""

        FOLLOW = "follow", _("Follow")
        BLOCK = "block", _("Block")

    relationship = models.fields.CharField(
        max_length=10, choices=RelationshipType.choices, null=True
    )
    remote_id = models.fields.RemoteIdField(null=True, unique=False)

    def start_job(self):
        """Start the job"""
        import_user_relationship_task.delay(child_id=self.id)


@app.task(queue=IMPORTS, base=ParentTask)
def start_import_task(**kwargs):
    """trigger the child import tasks for each user data
    We always import the books even if not assigning
    them to shelves, lists etc"""
    job = BookwyrmImportJob.objects.get(id=kwargs["job_id"])
    archive_file = job.bookwyrmimportjob.archive_file

    # don't start the job if it was stopped from the UI
    if job.complete:
        return

    job.status = "active"
    job.save(update_fields=["status"])

    try:
        archive_file.open("rb")
        with BookwyrmTarFile.open(mode="r:gz", fileobj=archive_file) as tar:
            json_filename = next(
                filter(lambda n: n.startswith("archive"), tar.getnames())
            )
            job.import_data = json.loads(tar.read(json_filename).decode("utf-8"))

            if "include_user_profile" in job.required:
                update_user_profile(job.user, tar, job.import_data)
            if "include_user_settings" in job.required:
                update_user_settings(job.user, job.import_data)
            if "include_goals" in job.required:
                update_goals(job.user, job.import_data.get("goals", []))
            if "include_saved_lists" in job.required:
                upsert_saved_lists(job.user, job.import_data.get("saved_lists", []))

            if "include_follows" in job.required:
                for remote_id in job.import_data.get("follows", []):
                    UserRelationshipImport.objects.create(
                        parent_job=job, remote_id=remote_id, relationship="follow"
                    )

            if "include_blocks" in job.required:
                for remote_id in job.import_data.get("blocks", []):
                    UserRelationshipImport.objects.create(
                        parent_job=job, remote_id=remote_id, relationship="block"
                    )

            for item in UserRelationshipImport.objects.filter(parent_job=job).all():
                item.start_job()

            for data in job.import_data.get("books"):
                book_job = UserImportBook.objects.create(parent_job=job, book_data=data)
                book_job.start_job()

        archive_file.close()
        # job.complete_job()

    except Exception as err:  # pylint: disable=broad-except
        logger.exception("User Import Job %s Failed with error: %s", job.id, err)
        job.set_status("failed")


@app.task(queue=IMPORTS, base=SubTask)
def import_book_task(**kwargs):
    """Take work and edition data,
    find or create the edition and work in the database"""

    task = UserImportBook.objects.get(id=kwargs["child_id"])
    job = task.parent_job
    archive_file = job.bookwyrmimportjob.archive_file
    book_data = task.book_data

    # don't start the job if it was stopped from the UI
    if job.complete or task.complete:
        return

    try:
        edition = book_data.get("edition")
        book = models.Edition.find_existing(edition)
        if not book:
            # make sure we have the authors in the local DB
            # replace the old author ids in the edition JSON
            edition["authors"] = []
            for author in book_data.get("authors"):
                parsed_author = activitypub.parse(author)
                instance = parsed_author.to_model(
                    model=models.Author,
                    save=True,
                    overwrite=True,  # TODO: why do we use overwrite?
                )

                edition["authors"].append(instance.remote_id)

            # we will add the cover later from the tar
            # don't try to load it from the old server
            cover = edition.get("cover", {})
            cover_path = cover.get("url", None)
            edition["cover"] = {}

            # first we need the parent work to exist
            work = book_data.get("work")
            work["editions"] = []
            parsed_work = activitypub.parse(work)
            work_instance = parsed_work.to_model(
                model=models.Work, save=True, overwrite=True
            )

            # now we have a work we can add it to the edition
            # and create the edition model instance
            edition["work"] = work_instance.remote_id
            parsed_edition = activitypub.parse(edition)
            book = parsed_edition.to_model(
                model=models.Edition, save=True, overwrite=True
            )

            # set the cover image from the tar
            # NOTE we don't have the images to go with test json!
            # TODO: test this later
            if cover_path:
                archive_file.open("rb")
                with BookwyrmTarFile.open(mode="r:gz", fileobj=archive_file) as tar:
                    tar.write_image_to_file(cover_path, book.cover)
                archive_file.close()

        task.book = book
        task.save(update_fields=["book"])
        required = task.parent_job.bookwyrmimportjob.required
        task_user = task.parent_job.user

        if "include_shelves" in required:
            upsert_shelves(task_user, book, book_data.get("shelves"))

        if "include_readthroughs" in required:
            upsert_readthroughs(task_user, book.id, book_data.get("readthroughs"))

        if "include_lists" in required:
            upsert_lists(task_user, book.id, book_data.get("lists"))

    except Exception as err:
        logger.exception(
            "Book Import Task %s for Job %s Failed with error: %s", task.id, job.id, err
        )
        job.set_status("failed")

    # Now import statuses
    # These are also subtasks so that we can isolate anything that fails
    if "include_comments" in job.bookwyrmimportjob.required:
        for status in book_data.get("comments"):
            UserImportPost.objects.create(
                parent_job=task.parent_job,
                json=status,
                book=book,
                status_type=UserImportPost.StatusType.COMMENT,
            )

    if "include_quotations" in job.bookwyrmimportjob.required:
        for status in book_data.get("quotations"):
            UserImportPost.objects.create(
                parent_job=task.parent_job,
                json=status,
                book=book,
                status_type=UserImportPost.StatusType.QUOTE,
            )

    if "include_reviews" in job.bookwyrmimportjob.required:
        for status in book_data.get("reviews"):
            UserImportPost.objects.create(
                parent_job=task.parent_job,
                json=status,
                book=book,
                status_type=UserImportPost.StatusType.REVIEW,
            )

    for item in UserImportPost.objects.filter(parent_job=job).all():
        item.start_job()

    task.complete_job()


@app.task(queue=IMPORTS, base=SubTask)
def upsert_statuses_task(**kwargs):
    """Find or create book statuses"""

    task = UserImportPost.objects.get(id=kwargs["child_id"])
    job = task.parent_job
    user = job.user
    status = task.json
    status_class = (
        models.Review
        if task.StatusType.REVIEW
        else models.Quotation
        if task.StatusType.QUOTE
        else models.Comment
    )

    # don't start the job if it was stopped from the UI
    if job.complete or task.complete:
        return

    try:
        # only add statuses if this is the same user
        logger.info("attributedTo: %s ", status.get("attributedTo", False))
        if is_alias(user, status.get("attributedTo", False)):
            status["attributedTo"] = user.remote_id
            status["to"] = update_followers_address(user, status["to"])
            status["cc"] = update_followers_address(user, status["cc"])
            status[
                "replies"
            ] = (
                {}
            )  # this parses incorrectly but we can't set it without knowing the new id
            status["inReplyToBook"] = task.book.remote_id
            parsed = activitypub.parse(status)
            if not status_already_exists(
                user, parsed
            ):  # don't duplicate posts on multiple import

                instance = parsed.to_model(
                    model=status_class, save=True, overwrite=True
                )

                for val in [
                    "progress",
                    "progress_mode",
                    "position",
                    "endposition",
                    "position_mode",
                ]:
                    if status.get(val):
                        instance.val = status[val]

                instance.remote_id = instance.get_remote_id()  # update the remote_id
                instance.save()  # save and broadcast

            task.complete_job()

        else:
            logger.warning(
                "User does not have permission to import statuses, or status is tombstone"
            )
            task.set_status("failed")

    except Exception as err:
        logger.exception("User Import Job %s Failed with error: %s", task.id, err)
        task.set_status("failed")


def upsert_readthroughs(user, book_id, data):
    """Take a JSON string of readthroughs and
    find or create the instances in the database"""

    for read_through in data:

        obj = {}
        keys = [
            "progress_mode",
            "start_date",
            "finish_date",
            "stopped_date",
            "is_active",
        ]
        for key in keys:
            obj[key] = read_through[key]
        obj["user_id"] = user.id
        obj["book_id"] = book_id

        existing = models.ReadThrough.objects.filter(**obj).first()
        if not existing:
            models.ReadThrough.objects.create(**obj)

    return


def upsert_lists(
    user,
    book_id,
    lists,
):
    """Take a list of objects each containing
    a list and list item as AP objects

    Because we are creating new IDs we can't assume the id
    will exist or be accurate, so we only use to_model for
    adding new items after checking whether they exist  .

    """

    book = models.Edition.objects.get(id=book_id)

    for blist in lists:
        booklist = models.List.objects.filter(name=blist["name"], user=user).first()
        if not booklist:

            blist["owner"] = user.remote_id
            parsed = activitypub.parse(blist)
            booklist = parsed.to_model(model=models.List, save=True, overwrite=True)

            booklist.privacy = blist["privacy"]
            booklist.save()

        item = models.ListItem.objects.filter(book=book, book_list=booklist).exists()
        if not item:
            count = booklist.books.count()
            models.ListItem.objects.create(
                book=book,
                book_list=booklist,
                user=user,
                notes=blist["list_item"]["notes"],
                approved=blist["list_item"]["approved"],
                order=count + 1,
            )

    return


def upsert_shelves(user, book, shelves):
    """Take shelf JSON objects and create
    DB entries if they don't already exist"""

    for shelf in shelves:

        book_shelf = models.Shelf.objects.filter(name=shelf["name"], user=user).first()

        if not book_shelf:
            book_shelf = models.Shelf.objects.create(name=shelf["name"], user=user)

        # add the book as a ShelfBook if needed
        if not models.ShelfBook.objects.filter(
            book=book, shelf=book_shelf, user=user
        ).exists():
            models.ShelfBook.objects.create(
                book=book, shelf=book_shelf, user=user, shelved_date=timezone.now()
            )

    return


# user updates
##############


def update_user_profile(user, tar, data):
    """update the user's profile from import data"""
    name = data.get("name", None)
    username = data.get("preferredUsername")
    user.name = name if name else username
    user.summary = strip_tags(data.get("summary", None))
    user.save(update_fields=["name", "summary"])
    if data["icon"].get("url"):
        avatar_filename = next(filter(lambda n: n.startswith("avatar"), tar.getnames()))
        tar.write_image_to_file(avatar_filename, user.avatar)


def update_user_settings(user, data):
    """update the user's settings from import data"""

    update_fields = ["manually_approves_followers", "hide_follows", "discoverable"]

    ap_fields = [
        ("manuallyApprovesFollowers", "manually_approves_followers"),
        ("hideFollows", "hide_follows"),
        ("discoverable", "discoverable"),
    ]

    for (ap_field, bw_field) in ap_fields:
        setattr(user, bw_field, data[ap_field])

    bw_fields = [
        "show_goal",
        "show_suggested_users",
        "default_post_privacy",
        "preferred_timezone",
    ]

    for field in bw_fields:
        update_fields.append(field)
        setattr(user, field, data["settings"][field])

    user.save(update_fields=update_fields)


def update_goals(user, data):
    """update the user's goals from import data"""

    for goal in data:
        # edit the existing goal if there is one
        existing = models.AnnualGoal.objects.filter(
            year=goal["year"], user=user
        ).first()
        if existing:
            for k in goal.keys():
                setattr(existing, k, goal[k])
            existing.save()
        else:
            goal["user"] = user
            models.AnnualGoal.objects.create(**goal)


def upsert_saved_lists(user, values):
    """Take a list of remote ids and add as saved lists"""

    for remote_id in values:
        book_list = activitypub.resolve_remote_id(remote_id, models.List)
        if book_list:
            user.saved_lists.add(book_list)


@app.task(queue=IMPORTS, base=SubTask)
def import_user_relationship_task(**kwargs):
    """import a user follow or block from an import file"""

    task = UserRelationshipImport.objects.get(id=kwargs["child_id"])
    job = task.parent_job

    try:
        if task.relationship == "follow":

            followee = activitypub.resolve_remote_id(task.remote_id, models.User)
            if followee:
                (
                    follow_request,
                    created,
                ) = models.UserFollowRequest.objects.get_or_create(
                    user_subject=job.user,
                    user_object=followee,
                )

                if not created:
                    # this request probably failed to connect with the remote
                    # and should save to trigger a re-broadcast
                    follow_request.save()

                task.complete_job()

            else:
                logger.exception(
                    "Could not resolve user %s task %s", task.remote_id, task.id
                )
                task.set_status("failed")

        elif tasks.relationship == "block":

            user_object = activitypub.resolve_remote_id(task.remote_id, models.User)
            if user_object:
                exists = models.UserBlocks.objects.filter(
                    user_subject=job.user, user_object=user_object
                ).exists()
                if not exists:
                    models.UserBlocks.objects.create(
                        user_subject=job.user, user_object=user_object
                    )
                    # remove the blocked users's lists from the groups
                    models.List.remove_from_group(job.user, user_object)
                    # remove the blocked user from all blocker's owned groups
                    models.GroupMember.remove(job.user, user_object)

                task.complete_job()

            else:
                logger.exception(
                    "Could not resolve user %s task %s", task.remote_id, task.id
                )
                task.set_status("failed")

        else:
            logger.exception(
                "Invalid relationship %s type specified in task %s",
                task.relationship,
                task.id,
            )
            task.set_status("failed")

    except IntegrityError as err:
        # `null value in column "to_user_id" of relation "bookwyrm_user_also_known_as" violates not-null constraint`
        # TODO: this seems to indicate that the *alias* doesn't have an ID, which will always be the case
        # if we don't have them in our DB already?
        # seems like a bug in activitypub.resolve_remote_id? We need to import BOTH users
        logger.exception(
            "User Import Job %s experienced an IntegrityError: %s", task.id, err
        )
        task.set_status("failed")
    except Exception as err:
        logger.exception("User Import Job %s Failed with error: %s", task.id, err)
        task.set_status("failed")


# utilities
###########


def update_followers_address(user, field):
    """statuses to or cc followers need to have the followers
    address updated to the new local user"""

    for i, audience in enumerate(field):
        if audience.rsplit("/")[-1] == "followers":
            field[i] = user.followers_url

    return field


def is_alias(user, remote_id):
    """check that the user is listed as moved_to
    or also_known_as in the remote user's profile"""

    if not remote_id:
        return False

    remote_user = activitypub.resolve_remote_id(
        remote_id=remote_id, model=models.User, save=False
    )

    if remote_user:
        if getattr(remote_user, "moved_to", None) is not None:
            return user.remote_id == remote_user.moved_to

        if hasattr(remote_user, "also_known_as"):
            return user in remote_user.also_known_as.all()

    return False


def status_already_exists(user, status):
    """check whether this status has already been published
    by this user. We can't rely on to_model() because it
    only matches on remote_id, which we have to change
    *after* saving because it needs the primary key (id)"""

    return models.Status.objects.filter(
        user=user, content=status.content, published_date=status.published
    ).exists()
