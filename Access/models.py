from django.contrib.auth.models import User as user
from django.db import models, transaction
from django.db.models.signals import post_save
from django.conf import settings
from EnigmaAutomation.settings import PERMISSION_CONSTANTS
import datetime
import enum


class StoredPassword(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        editable=False
    )
    password = models.CharField(
        'Password hash',
        max_length=255,
        editable=False
    )
    date = models.DateTimeField(
        'Date',
        auto_now_add=True,
        editable=False
    )

class ApprovalType(enum.Enum):
    Primary = "Primary"
    Secondary = "Secondary"


class Permission(models.Model):
    """
    Permission to perform actions on enigma
    """

    label = models.CharField(max_length=255, null=False, blank=False, unique=True)

    def __str__(self):
        return "%s" % (self.label)


class Role(models.Model):
    """
    User role to attach permissions to perform actions on enigma; one user can have multiple roles
    Role is a group of permissions which can be associated with a group of users
    """

    label = models.CharField(max_length=255, null=False, blank=False, unique=True)
    permission = models.ManyToManyField(Permission)

    def __str__(self):
        return "%s" % (self.label)


class SshPublicKey(models.Model):
    """
    SSH Public keys for users
    """

    key = models.TextField(null=False, blank=False)

    STATUS_CHOICES = (("Active", "active"), ("Revoked", "revoked"))
    status = models.CharField(
        max_length=100,
        null=False,
        blank=False,
        choices=STATUS_CHOICES,
        default="Active",
    )

    def __str__(self):
        return str(self.key)


# Create your models here.
class User(models.Model):
    """
    Represents an user belonging to the organistaion
    """

    user = models.OneToOneField(
        user, null=False, blank=False, on_delete=models.CASCADE, related_name="user"
    )
    name = models.CharField(max_length=255, null=True, blank=False)

    email = models.EmailField(null=True, blank=False)
    phone = models.IntegerField(null=True, blank=True)

    is_bot = models.BooleanField(null=False, blank=False, default=False)
    BOT_TYPES = (
        ("None", "none"),
        ("Github", "github"),
    )
    bot_type = models.CharField(
        max_length=100, null=False, blank=False, choices=BOT_TYPES, default="None"
    )

    alerts_enabled = models.BooleanField(null=False, blank=False, default=False)

    is_manager = models.BooleanField(null=False, blank=False, default=False)
    is_ops = models.BooleanField(null=False, blank=False, default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    avatar = models.TextField(null=True, blank=True)

    USER_STATUS_CHOICES = [
        ("1", "active"),
        ("2", "offboarding"),
        ("3", "offboarded"),
    ]

    state = models.CharField(
        max_length=255, null=False, blank=False, choices=USER_STATUS_CHOICES, default=1
    )
    role = models.ManyToManyField(Role, blank=True)

    offbaord_date = models.DateTimeField(null=True, blank=True)
    revoker = models.ForeignKey(
        "User",
        null=True,
        blank=True,
        related_name="user_revoker",
        on_delete=models.PROTECT,
    )

    @property
    def permissions(self):
        user_roles = self.role.all()
        user_permissions = [
            permission for role in user_roles for permission in role.permission.all()
        ]
        return user_permissions

    def has_permission(self, permission_label):
        all_permission_labels = [permission.label for permission in self.permissions]
        return permission_label in all_permission_labels

    def current_state(self):
        return dict(self.USER_STATUS_CHOICES).get(self.state)

    def change_state(self, final_state):
        user_states = dict(self.USER_STATUS_CHOICES)
        state_key = self.state
        for key in user_states:
            if user_states[key] == final_state:
                state_key = key
        self.state = state_key
        self.save()

    def isAnApprover(self, allApproverPermissions):
        permission_labels = [permission.label for permission in self.permissions]
        approver_permissions = allApproverPermissions
        return len(list(set(permission_labels) & set(approver_permissions))) > 0

    def isPrimaryApproverForModule(self, accessModule, accessLabel=None):
        module_permissions = accessModule.fetch_approver_permissions(accessLabel)
        return self.has_permission(module_permissions["1"])

    def isSecondaryApproverForModule(self, accessModule, accessLabel=None):
        module_permissions = accessModule.fetch_approver_permissions(accessLabel)
        return "2" in module_permissions and self.has_permission(
            module_permissions["2"]
        )

    def isAnApproverForModule(
        self, accessModule, accessLabel=None, approverType="Primary"
    ):
        if approverType == "Secondary":
            return self.isSecondaryApproverForModule(accessModule, accessLabel)

        return self.isPrimaryApproverForModule(accessModule, accessLabel)

    def getPendingApprovalsCount(self, all_access_modules):
        pendingCount = 0
        if self.has_permission(PERMISSION_CONSTANTS["DEFAULT_APPROVER_PERMISSION"]):
            pendingCount += GroupV2.getPendingMemberships().count()
            pendingCount += len(GroupV2.getPendingCreation())

        for each_tag, each_access_module in all_access_modules.items():
            all_requests = each_access_module.get_pending_access_objects(self)
            pendingCount += len(all_requests["individual_requests"])
            pendingCount += len(all_requests["group_requests"])

        return pendingCount

    def getFailedGrantsCount(self):
        return (
            UserAccessMapping.objects.filter(status__in=["GrantFailed"]).count()
            if self.isAdminOrOps()
            else 0
        )

    def getFailedRevokesCount(self):
        return (
            UserAccessMapping.objects.filter(status__in=["RevokeFailed"]).count()
            if self.isAdminOrOps()
            else 0
        )

    def getOwnedGroups(self):
        if self.isAdminOrOps():
            return GroupV2.objects.all().filter(status="Approved")

        groupOwnerMembership = MembershipV2.objects.filter(is_owner=True, user=self)
        return [membership_obj.group for membership_obj in groupOwnerMembership]

    def isAdminOrOps(self):
        return self.is_ops or self.user.is_superuser

    def get_all_approved_memberships(self):
        return self.membership_user.filter(status="Approved")

    def is_allowed_admin_actions_on_group(self, group):
        return (
            group.member_is_owner(self) or self.isAdminOrOps()
        )

    def is_allowed_to_offboard_user_from_group(self, group):
        return group.member_is_owner(self) or self.has_permission("ALLOW_USER_OFFBOARD")

    def create_new_identity(self, access_tag="", identity=""):
        return self.module_identity.create(access_tag=access_tag, identity=identity)

    def get_active_identity(self, access_tag):
        return self.module_identity.filter(
            access_tag=access_tag, status="Active"
        ).first()

    def get_all_active_identity(self):
        return self.module_identity.filter(status="Active")

    def is_active(self):
        return self.current_state() == "active"

    @staticmethod
    def get_user_by_email(email):
        try:
            return User.objects.get(email=email)
        except User.DoesNotExist:
            return None

    def get_user_access_mappings(self):
        all_user_identities = self.module_identity.all()
        access_request_mappings = []
        for each_identity in all_user_identities:
            access_request_mappings.extend(
                each_identity.user_access_mapping.prefetch_related(
                    "access", "approver_1", "approver_2"
                )
            )
        return access_request_mappings

    def get_access_history(self, all_access_modules):
        access_request_mappings = self.get_user_access_mappings()
        access_history = []

        for request_mapping in access_request_mappings:
            access_module = all_access_modules[request_mapping.access.access_tag]
            access_history.append(
                request_mapping.getAccessRequestDetails(access_module)
            )

        return access_history

    @staticmethod
    def get_user_from_username(username):
        try:
            return User.objects.get(user__username=username)
        except User.DoesNotExist:
            return None

    def get_accesses_by_access_tag_and_status(self, access_tag, status):
        try:
            user_identities = self.module_identity.filter(access_tag=access_tag)
        except UserIdentity.DoesNotExist:
            return None
        return UserAccessMapping.objects.filter(
            user_identity__in=user_identities,
            access__access_tag=access_tag,
            status__in=status,
        )

    def update_revoker(self, revoker):
        self.revoker = revoker
        self.save()

    def offboard(self, revoker):
        self.change_state("offboarding")
        self.update_revoker(revoker)
        self.offbaord_date = datetime.datetime.now()
        self.user.is_active = False
        self.save()

    def revoke_all_memberships(self):
        self.membership_user.filter(status__in=["Pending", "Approved"]).update(
            status="Revoked"
        )

    def get_or_create_active_identity(self, access_tag):
        identity, created = self.module_identity.get_or_create(
            access_tag=access_tag, status="Active"
        )
        return identity

    @staticmethod
    def get_users_by_emails(emails):
        return User.objects.filter(email__in=emails)

    @staticmethod
    def get_user_by_email(email):
        try:
            return User.objects.get(email=email)
        except User.DoesNotExist:
            return None

    @staticmethod
    def get_active_users_with_permission(permission_label):
        try:
            return User.objects.filter(
                role__permission__label=permission_label, state=1
            )
        except User.DoesNotExist:
            return None

    @staticmethod
    def get_system_user():
        try:
            return User.objects.get(name="system_user")
        except User.DoesNotExist:
            django_user = user.objects.create(username="system_user",email="system_user@root.root")
            return django_user.user

    def __str__(self):
        return "%s" % (self.user)

def create_user(sender, instance, created, **kwargs):
    """
    create a user when a django  user is created
    """
    user, created = User.objects.get_or_create(user=instance)
    user.name = instance.first_name
    user.email = instance.email
    try:
        user.avatar = instance.avatar
    except Exception as e:
        pass
    user.save()

post_save.connect(create_user, sender=user)


class MembershipV2(models.Model):
    """
    Membership of user in a GroupV2
    """

    membership_id = models.CharField(
        max_length=255, null=False, blank=False, unique=True
    )

    user = models.ForeignKey(
        "User",
        null=False,
        blank=False,
        related_name="membership_user",
        on_delete=models.PROTECT,
    )
    group = models.ForeignKey(
        "GroupV2",
        null=False,
        blank=False,
        related_name="membership_group",
        on_delete=models.PROTECT,
    )
    is_owner = models.BooleanField(null=False, blank=False, default=False)

    requested_by = models.ForeignKey(
        User,
        null=False,
        blank=False,
        related_name="membership_requester",
        on_delete=models.PROTECT,
    )
    requested_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    STATUS = (
        ("Pending", "pending"),
        ("Approved", "approved"),
        ("Declined", "declined"),
        ("Revoked", "revoked"),
    )
    status = models.CharField(
        max_length=255, null=False, blank=False, choices=STATUS, default="Pending"
    )
    reason = models.TextField(null=True, blank=True)

    approver = models.ForeignKey(
        "User",
        null=True,
        blank=True,
        related_name="membership_approver",
        on_delete=models.PROTECT,
    )
    decline_reason = models.TextField(null=True, blank=True)

    def deactivate(self):
        self.status = "Revoked"
        self.save()

    def approve(self, approver):
        self.status = "Approved"
        self.approver = approver
        self.save()

    def unapprove(self):
        self.status = "Pending"
        self.approver = None
        self.save()

    def get_status(self):
        return self.status

    def is_self_approval(self, approver):
        return self.requested_by == approver

    def is_pending(self):
        return self.status == "Pending"

    @staticmethod
    def approve_membership(membership_id, approver):
        membership = MembershipV2.objects.get(membership_id=membership_id)
        membership.approve(approver=approver)

    def decline(self, reason, decliner):
        self.status = "Declined"
        self.decline_reason = reason
        self.approver = decliner
        self.save()

    def is_already_processed(self):
        return self.status in ["Declined", "Approved", "Processing", "Revoked"]

    def revoke_membership(self):
        self.status = "Revoked"
        self.save()

    def update_membership(group, reason):
        membership = MembershipV2.objects.filter(group=group)
        membership.update(status="Declined", decline_reason=reason)

    @staticmethod
    def get_membership(membership_id):
        try:
            return MembershipV2.objects.get(membership_id=membership_id)
        except MembershipV2.DoesNotExist:
            return None

    def __str__(self):
        return self.group.name + "-" + self.user.email + "-" + self.status


class GroupV2(models.Model):
    """
    Model for Enigma Groups redefined.
    """

    group_id = models.CharField(max_length=255, null=False, blank=False, unique=True)
    requested_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    name = models.CharField(max_length=128, null=False, blank=False, unique=True)
    description = models.TextField(null=False, blank=False)

    requester = models.ForeignKey(
        "User",
        null=True,
        blank=True,
        related_name="group_requester",
        on_delete=models.PROTECT,
    )

    STATUS = (
        ("Pending", "pending"),
        ("Approved", "approved"),
        ("Declined", "declined"),
        ("Deprecated", "deprecated"),
    )
    status = models.CharField(
        max_length=255, null=False, blank=False, choices=STATUS, default="Pending"
    )

    approver = models.ForeignKey(
        "User",
        null=True,
        blank=True,
        related_name="group_approver",
        on_delete=models.PROTECT,
    )
    decline_reason = models.TextField(null=True, blank=True)
    needsAccessApprove = models.BooleanField(null=False, blank=False, default=True)

    @staticmethod
    def group_exists(group_name):
        if len(
            GroupV2.objects.filter(name=group_name).filter(
                status__in=["Approved", "Pending"]
            )
        ):
            return True
        return False

    @staticmethod
    def create(
        name="", requester=None, description="", needsAccessApprove=True, date_time=""
    ):
        return GroupV2.objects.create(
            name=name,
            group_id=name + "-group-" + date_time,
            requester=requester,
            description=description,
            needsAccessApprove=needsAccessApprove,
        )

    def add_member(
        self, user=None, is_owner=False, requested_by=None, reason="", date_time=""
    ):
        membership_id = (
            str(user.user.username) + "-" + self.name + "-membership-" + date_time
        )
        return self.membership_group.create(
            membership_id=membership_id,
            user=user,
            is_owner=is_owner,
            requested_by=requested_by,
            reason=reason,
        )

    def add_members(self, users=None, requested_by=None, reason="", date_time=""):
        if users:
            for usr in users:
                self.add_member(
                    user=usr,
                    requested_by=requested_by,
                    reason=reason,
                    date_time=date_time,
                )

    def getPendingMemberships():
        return MembershipV2.objects.filter(status="Pending", group__status="Approved")

    def is_already_processed(self):
        return self.status in ['Declined','Approved','Processing','Revoked']

    def decline_access(self, decline_reason=None):
        self.status = "Declined"
        self.decline_reason = decline_reason
        self.save()

    @staticmethod
    def getPendingCreation():
        new_group_pending = GroupV2.objects.filter(status="Pending")
        new_group_pending_data = []
        for new_group in new_group_pending:
            initial_members = ", ".join(
                list(
                    new_group.membership_group.values_list(
                        "user__user__username", flat=True
                    )
                )
            )
            new_group_pending_data.append(
                {"groupRequest": new_group, "initialMembers": initial_members}
            )
        return new_group_pending_data

    @staticmethod
    def get_pending_group(group_id):
        try:
            return GroupV2.objects.get(group_id=group_id, status="Pending")
        except GroupV2.DoesNotExist:
            return None

    @staticmethod
    def get_approved_group(group_id):
        try:
            return GroupV2.objects.get(group_id=group_id, status="Approved")
        except GroupV2.DoesNotExist:
            return None

    @staticmethod
    def get_active_group_by_name(group_name):
        try:
            return GroupV2.objects.get(name=group_name, status="Approved")
        except GroupV2.DoesNotExist:
            return None

    @staticmethod
    def get_approved_group_by_name(group_name):
        try:
            return GroupV2.objects.filter(name=group_name, status="Approved").first()
        except GroupV2.DoesNotExist:
            return None

    def approve_all_pending_users(self, approved_by):
        self.membership_group.filter(status="Pending").update(
            status="Approved", approver=approved_by
        )

    def get_all_members(self):
        group_members = self.membership_group.all()
        return group_members

    def get_all_approved_members(self):
        group_members = self.get_all_members().filter(status="Approved")
        return group_members

    def get_approved_and_pending_member_emails(self):
        group_member_emails = self.membership_group.filter(
            status__in=["Approved", "Pending"]
        ).values_list("user__email", flat=True)
        return group_member_emails

    def member_is_owner(self, user):
        try:
            membership = self.membership_group.get(user=user)
        except MembershipV2.DoesNotExist:
            return False
        return membership.is_owner

    def get_active_accesses(self):
        return self.group_access_mapping.filter(
            status__in=["Approved", "Pending", "Declined", "SecondaryPending"]
        )

    def is_self_approval(self, approver):
        return self.requester == approver

    def approve(self, approved_by):
        self.approver = approved_by
        self.status = "Approved"
        self.save()

    def unapprove(self):
        self.approver = None
        self.status = "Pending"
        self.save()

    def unapprove_memberships(self):
        self.membership_group.filter(status="Approved").update(
            status="Pending", approver=None
        )

    def is_owner(self, user):
        return (
            self.membership_group.filter(is_owner=True)
            .filter(user=user)
            .first()
            is not None
        )

    def add_access(self, request_id, requested_by, request_reason, access):
        self.group_access_mapping.create(
            request_id=request_id,
            requested_by=requested_by,
            request_reason=request_reason,
            access=access,
        )

    def check_access_exist(self, access):
        try:
            self.group_access_mapping.get(access=access)
            return True
        except GroupAccessMapping.DoesNotExist:
            return False

    def get_all_approved_members(self):
        return self.membership_group.filter(status="Approved")

    def get_approved_accesses(self):
        return self.group_access_mapping.filter(status="Approved")

    def is_owner(self, email):
        return (
            self.membership_group.filter(is_owner=True)
            .filter(user__email=email)
            .first()
            is not None
        )

    def __str__(self):
        return self.name


class UserAccessMapping(models.Model):
    """
    Model to map access to user. Requests are broken down
    into mappings which are sent for approval.
    """

    request_id = models.CharField(max_length=255, null=False, blank=False, unique=True)

    requested_on = models.DateTimeField(auto_now_add=True)
    approved_on = models.DateTimeField(null=True, blank=True)
    updated_on = models.DateTimeField(auto_now=True)

    request_reason = models.TextField(null=False, blank=False)

    approver_1 = models.ForeignKey(
        "User",
        null=True,
        blank=True,
        related_name="approver_1",
        on_delete=models.PROTECT,
    )
    approver_2 = models.ForeignKey(
        "User",
        null=True,
        blank=True,
        related_name="approver_2",
        on_delete=models.PROTECT,
    )

    access = models.ForeignKey(
        "AccessV2", null=False, blank=False, on_delete=models.PROTECT
    )

    STATUS_CHOICES = (
        ("Pending", "pending"),
        ("SecondaryPending", "secondarypending"),
        ("Processing", "processing"),
        ("Approved", "approved"),
        ("GrantFailed", "grantfailed"),
        ("Declined", "declined"),
        ("Offboarding", "offboarding"),
        ("ProcessingRevoke", "processingrevoke"),
        ("RevokeFailed", "revokefailed"),
        ("Revoked", "revoked"),
    )
    status = models.CharField(
        max_length=100,
        null=False,
        blank=False,
        choices=STATUS_CHOICES,
        default="Pending",
    )

    decline_reason = models.TextField(null=True, blank=True)

    fail_reason = models.TextField(null=True, blank=True)

    TYPE_CHOICES = (("Individual", "individual"), ("Group", "group"))
    access_type = models.CharField(
        max_length=255,
        null=False,
        blank=False,
        choices=TYPE_CHOICES,
        default="Individual",
    )
    revoker = models.ForeignKey(
        "User",
        null=True,
        blank=True,
        related_name="user_access_revoker",
        on_delete=models.PROTECT,
    )
    meta_data = models.JSONField(default=dict, blank=True, null=True)

    user_identity = models.ForeignKey(
        "UserIdentity",
        null=True,
        blank=True,
        related_name="user_access_mapping",
        on_delete=models.PROTECT,
    )

    def __str__(self):
        return self.request_id

    # Wrote the override version of save method in order to update the
    # "approved_on" field whenever the request is marked "Approved"
    def save(self, *args, **kwargs):
        super(UserAccessMapping, self).save(*args, **kwargs)
        # Consider only the first cycle of approval
        if self.status.lower() == "approved" and self.approved_on in [None, ""]:
            self.approved_on = self.updated_on
            super(UserAccessMapping, self).save(*args, **kwargs)

    @staticmethod
    def get_access_request(request_id):
        try:
            return UserAccessMapping.objects.get(request_id=request_id)
        except UserAccessMapping.DoesNotExist:
            return None

    def getAccessRequestDetails(self, access_module):
        access_request_data = {}
        access_tags = [self.access.access_tag]
        access_labels = [self.access.access_label]

        access_tag = access_tags[0]
        # code metadata
        access_request_data["access_tag"] = access_tag
        # ui metadata
        access_request_data["user"] = self.user_identity.user.name
        access_request_data["userEmail"] = self.user_identity.user.email
        access_request_data["requestId"] = self.request_id
        access_request_data["accessReason"] = self.request_reason
        access_request_data["requested_on"] = self.requested_on

        access_request_data["access_desc"] = access_module.access_desc()
        access_request_data["accessCategory"] = access_module.combine_labels_desc(
            access_labels
        )
        access_request_data["accessMeta"] = access_module.combine_labels_meta(
            access_labels
        )
        access_request_data["access_label"] = [
            key + "-" + str(val).strip("[]")
            for key, val in list(self.access.access_label.items())
            if key != "keySecret"
        ]
        access_request_data["access_type"] = self.access_type
        access_request_data["approver_1"] = (
            self.approver_1.user.username if self.approver_1 else ""
        )
        access_request_data["approver_2"] = (
            self.approver_2.user.username if self.approver_2 else ""
        )
        access_request_data["approved_on"] = (
            self.approved_on if self.approved_on else ""
        )
        access_request_data["updated_on"] = (
            str(self.updated_on)[:19] + "UTC" if self.updated_on else ""
        )
        access_request_data["status"] = self.status
        access_request_data["revoker"] = (
            self.revoker.user.username if self.revoker else ""
        )
        access_request_data["offboarding_date"] = (
            str(self.user_identity.user.offbaord_date)[:19] + "UTC"
            if self.user_identity.user.offbaord_date
            else ""
        )
        access_request_data["revokeOwner"] = ",".join(access_module.revoke_owner())
        access_request_data["grantOwner"] = ",".join(access_module.grant_owner())

        return access_request_data

    def update_meta_data(self, key, data):
        with transaction.atomic():
            self.meta_data[key] = data
            self.save()
        return True

    def revoke(self, revoker=None):
        self.status = "Revoked"
        if revoker:
            self.revoker = revoker
        self.save()

    @staticmethod
    def get_accesses_not_declined():
        return UserAccessMapping.objects.exclude(status="Declined")

    @staticmethod
    def get_unrevoked_accesses_by_request_id(request_id):
        return UserAccessMapping.objects.filter(request_id=request_id).exclude(
            status="Revoked"
        )

    def is_approved(self):
        return self.status == "Approved"

    def is_processing(self):
        return self.status == "Processing"

    def is_pending(self):
        return self.status == "Pending"

    def is_secondary_pending(self):
        return self.status == "SecondaryPending"

    def is_grantfailed(self):
        return self.status == "GrantFailed"

    def decline_access(self, decline_reason=None):
        self.status = "Declined"
        self.decline_reason = decline_reason
        self.save()

    @staticmethod
    def get_pending_access_mapping(request_id):
        return UserAccessMapping.objects.filter(
            request_id__icontains=request_id, status__in=["Pending", "SecondaryPending"]
        ).values_list("request_id", flat=True)

    def update_access_status(self, current_status):
        self.status = current_status
        self.save()

    def is_already_processed(self):
        return self.status in ["Declined", "Approved", "Processing", "Revoked"]

    def grant_fail_access(self, fail_reason=None):
        self.status = "GrantFailed"
        self.fail_reason = fail_reason
        self.save()

    def revoke_failed(self, fail_reason=None):
        self.status = "RevokeFailed"
        self.fail_reason = fail_reason
        self.save()

    def decline_access(self, decline_reason=None):
        self.status = "Declined"
        self.decline_reason = decline_reason
        self.save()

    def approve_access(self):
        self.status = "Approved"
        self.save()

    def revoking(self, revoker):
        self.revoker = revoker
        self.status = "ProcessingRevoke"
        self.save()

    def processing(self, approval_type, approver):
        if approval_type == ApprovalType.Primary:
            self.approver_1 = approver
        elif approval_type == ApprovalType.Secondary:
            self.approver_2 = approver
        else:
            raise Exception("Invalid ApprovalType")
        self.status = "Processing"
        self.save()

    @staticmethod
    def create(
        request_id,
        user_identity,
        access,
        approver_1,
        approver_2,
        request_reason,
        access_type,
        status,
    ):
        mapping = UserAccessMapping(
            request_id=request_id,
            user_identity=user_identity,
            access=access,
            approver_1=approver_1,
            approver_2=approver_2,
            request_reason=request_reason,
            access_type=access_type,
            status=status,
        )
        mapping.save()
        return mapping

    def get_user_name(self):
        return self.user_identity.user.name


class GroupAccessMapping(models.Model):
    """
    Model to map access to group. Requests are broken down
    into mappings which are sent for approval.
    """

    request_id = models.CharField(max_length=255, null=False, blank=False, unique=True)

    requested_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    group = models.ForeignKey(
        "GroupV2",
        null=False,
        blank=False,
        on_delete=models.PROTECT,
        related_name="group_access_mapping",
    )

    requested_by = models.ForeignKey(
        "User",
        null=True,
        blank=False,
        related_name="g_requester",
        on_delete=models.PROTECT,
    )

    request_reason = models.TextField(null=False, blank=False)

    approver_1 = models.ForeignKey(
        "User",
        null=True,
        blank=True,
        related_name="g_approver_1",
        on_delete=models.PROTECT,
    )
    approver_2 = models.ForeignKey(
        "User",
        null=True,
        blank=True,
        related_name="g_approver_2",
        on_delete=models.PROTECT,
    )

    access = models.ForeignKey(
        "AccessV2", null=False, blank=False, on_delete=models.PROTECT
    )

    STATUS_CHOICES = (
        ("Pending", "pending"),
        ("SecondaryPending", "secondarypending"),
        ("Approved", "approved"),
        ("Declined", "declined"),
        ("Revoked", "revoked"),
        ("Inactive", "inactive"),
    )
    status = models.CharField(
        max_length=100,
        null=False,
        blank=False,
        choices=STATUS_CHOICES,
        default="Pending",
    )

    decline_reason = models.TextField(null=True, blank=True)

    revoker = models.ForeignKey(
        "User",
        null=True,
        blank=True,
        related_name="group_access_revoker",
        on_delete=models.PROTECT,
    )

    def __str__(self):
        return self.request_id

    def getAccessRequestDetails(self, access_module):
        access_request_data = {}
        access_tags = [self.access.access_tag]
        access_labels = [self.access.access_label]

        access_tag = access_tags[0]
        # code metadata
        access_request_data["access_tag"] = access_tag
        # ui metadata
        access_request_data["userEmail"] = self.requested_by.email
        access_request_data["groupName"] = self.group.name
        access_request_data["requestId"] = self.request_id
        access_request_data["accessReason"] = self.request_reason
        access_request_data["requested_on"] = self.requested_on

        access_request_data["accessType"] = access_module.access_desc()
        access_request_data["accessCategory"] = access_module.combine_labels_desc(
            access_labels
        )
        access_request_data["accessMeta"] = access_module.combine_labels_meta(
            access_labels
        )
        access_request_data["status"] = self.status
        access_request_data["revokeOwner"] = ",".join(access_module.revoke_owner())
        access_request_data["grantOwner"] = ",".join(access_module.grant_owner())

        return access_request_data

    def get_by_id(request_id):
        try:
            return GroupAccessMapping.objects.get(request_id=request_id)
        except GroupAccessMapping.DoesNotExist:
            return None

    def mark_revoked(self, revoker):
        self.status = "Revoked"
        self.revoker = revoker
        self.save()



    @staticmethod
    def get_by_request_id(request_id):
        try:
            return GroupAccessMapping.objects.get(request_id=request_id)
        except GroupAccessMapping.DoesNotExist:
            return None

    @staticmethod
    def get_pending_access_mapping(request_id):
        return GroupAccessMapping.objects.filter(
            request_id__icontains=request_id, status__in=["Pending", "SecondaryPending"]
        ).values_list("request_id", flat=True)

    def is_pending(self):
        return self.status == "Pending"

    def is_secondary_pending(self):
        return self.status == "SecondaryPending"

    def set_primary_approver(self, approver):
        self.approver_1 = approver
        self.save()

    def set_secondary_approver(self, approver):
        self.approver_2 = approver
        self.save()

    def get_primary_approver(self):
        return self.approver_1

    def get_secondary_approver(self):
        return self.approver_2

    def approve_access(self):
        self.status = "Approved"
        self.save()

    def decline_access(self, decline_reason):
        self.status = "Declined"
        self.decline_reason = decline_reason
        self.save()

    def update_access_status(self, current_status):
        self.status = current_status
        self.save()

    def is_self_approval(self, approver):
        return self.requested_by == approver

    def is_already_processed(self):
        return self.status in ['Declined','Approved','Processing','Revoked']


class AccessV2(models.Model):
    access_tag = models.CharField(max_length=255)
    access_label = models.JSONField(default=dict)
    is_auto_approved = models.BooleanField(null=False, default=False)

    def __str__(self):
        try:
            details_arr = []
            for data in list(self.access_label.values()):
                try:
                    details_arr.append(data.decode("utf-8"))
                except Exception:
                    details_arr.append(data)
            return self.access_tag + " - " + ", ".join(details_arr)
        except Exception:
            return self.access_tag

    @staticmethod
    def get(access_tag, access_label):
        try:
            return AccessV2.objects.get(
                access_tag=access_tag, access_label=access_label
            )
        except AccessV2.DoesNotExist:
            return None

    @staticmethod
    def create(access_tag, access_label):
        return AccessV2.objects.create(access_tag=access_tag, access_label=access_label)


class UserIdentity(models.Model):
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "access_tag", "status"],
                condition=models.Q(status="Active"),
                name="one_active_identity_per_access_module_per_user",
            )
        ]

    access_tag = models.CharField(max_length=255)

    user = models.ForeignKey(
        "User",
        null=False,
        blank=False,
        related_name="module_identity",
        on_delete=models.PROTECT,
    )
    identity = models.JSONField(default=dict)

    STATUS_CHOICES = (
        ("Active", "active"),
        ("Inactive", "inactive"),
    )

    status = models.CharField(
        max_length=100,
        null=False,
        blank=False,
        choices=STATUS_CHOICES,
        default="Active",
    )

    def deactivate(self):
        self.status = "Inactive"
        self.save()

    def get_active_access_mapping(self):
        return self.user_access_mapping.filter(
            status__in=["Approved", "Pending",
                        "SecondaryPending",
                        "GrantFailed"],
            access__access_tag=self.access_tag
        )

    def get_all_granted_access_mappings(self):
        return self.user_access_mapping.filter(
            status__in=["Approved", "Processing", "Offboarding"],
            access__access_tag=self.access_tag,
        )

    def get_all_non_approved_access_mappings(self):
        return self.user_access_mapping.filter(
            status__in=["Pending", "SecondaryPending", "GrantFailed"]
        )

    def decline_all_non_approved_access_mappings(self, decline_reason):
        user_mapping = self.get_all_non_approved_access_mappings()
        user_mapping.update(status="Declined", decline_reason=decline_reason)

    def get_granted_access_mapping(self, access):
        return self.user_access_mapping.filter(
            status__in=["Approved", "Processing", "Offboarding"], access=access
        )

    def get_non_approved_access_mapping(self, access):
        return self.user_access_mapping.filter(
            status__in=["Pending", "SecondaryPending", "GrantFailed"],
            access=access,
        )

    def decline_non_approved_access_mapping(self, access, decline_reason):
        user_mapping = self.get_non_approved_access_mapping(access)
        user_mapping.update(status="Declined", decline_reason=decline_reason)

    def offboarding_approved_access_mapping(self, access):
        user_mapping = self.get_granted_access_mapping(access)
        user_mapping.update(status="Offboarding")

    def revoke_approved_access_mapping(self, access):
        user_mapping = self.get_granted_access_mapping(access)
        user_mapping.update(status="Revoked")

    def mark_revoke_failed_for_approved_access_mapping(self, access):
        user_mapping = self.get_granted_access_mapping(access)
        user_mapping.update(status="RevokeFailed")

    def access_mapping_exists(self, access):
        try:
            self.user_access_mapping.get(
                access=access, status__in=["Approved", "Pending"]
            )
            return True
        except Exception:
            return False

    def replicate_active_access_membership_for_module(
        self, existing_user_access_mapping
    ):
        new_user_access_mapping = []

        for i, user_access in enumerate(existing_user_access_mapping):
            base_datetime_prefix = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
            request_id = (
                self.user.user.username
                + "-"
                + user_access.access_type
                + "-"
                + base_datetime_prefix
                + "-"
                + str(i)
            )
            access_status = user_access.status
            if user_access.status.lower() == "approved":
                access_status = "Processing"

            new_user_access_mapping.append(
                self.user_access_mapping.create(
                    request_id=request_id,
                    access=user_access.access,
                    approver_1=user_access.approver_1,
                    approver_2=user_access.approver_2,
                    request_reason=user_access.request_reason,
                    access_type=user_access.access_type,
                    status=access_status,
                )
            )
        return new_user_access_mapping

    def create_access_mapping(
        self,
        request_id,
        access,
        approver_1,
        approver_2,
        reason,
        access_type="Individual",
    ):
        return self.user_access_mapping.create(
            request_id=request_id,
            access=access,
            approver_1=approver_1,
            approver_2=approver_2,
            request_reason=reason,
            access_type=access_type,
        )

    def has_approved_access(self, access):
        return self.user_access_mapping.filter(
            status="Approved", access=access
        ).exists()

    def __str__(self):
        return "%s" % (self.identity)
