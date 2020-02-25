

import base64
import datetime
import random
import re
import string
from hashlib import md5
from itertools import chain

import cachemodel
from allauth.account.models import EmailAddress, EmailConfirmation
from backpack.models import BackpackCollection
from badgeuser.managers import CachedEmailAddressManager, BadgeUserManager, EmailAddressCacheModelManager
from basic_models.models import IsActive
from django.conf import settings
from django.contrib.auth.models import AbstractUser, Permission, Group
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import models, transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from entity.models import BaseVersionedEntity
from issuer.models import Issuer, BadgeInstance, BaseAuditedModel
from lti_edu.models import StudentsEnrolled
from mainsite.models import ApplicationInfo, EmailBlacklist, BadgrApp
from mainsite.utils import generate_entity_uri
from oauth2_provider.models import AccessToken, Application
from oauthlib.common import generate_token
from rest_framework.authtoken.models import Token
from signing.models import AssertionTimeStamp


class CachedEmailAddress(EmailAddress, cachemodel.CacheModel):
    objects = CachedEmailAddressManager()
    cached = EmailAddressCacheModelManager()

    class Meta:
        proxy = True
        verbose_name = _("email address")
        verbose_name_plural = _("email addresses")

    def generate_forgot_password_time_cache_key(self):
        return "{}_forgot_request_date".format(self.email)

    def get_last_forgot_password_sent_time(self):
        cached_time = cache.get(self.generate_forgot_password_time_cache_key())
        return cached_time

    def set_last_forgot_password_sent_time(self, new_datetime):
        cache.set(self.generate_forgot_password_time_cache_key(), new_datetime)

    def generate_verification_time_cache_key(self):
        return "{}_verification_request_date".format(self.email)

    def get_last_verification_sent_time(self):
        cached_time = cache.get(self.generate_verification_time_cache_key())
        return cached_time

    def set_last_verification_sent_time(self, new_datetime):
        cache.set(self.generate_verification_time_cache_key(), new_datetime)

    def publish(self):
        super(CachedEmailAddress, self).publish()
        self.publish_by('email')
        self.user.publish()

    def delete(self, *args, **kwargs):
        user = self.user
        self.publish_delete('email')
        self.publish_delete('pk')
        super(CachedEmailAddress, self).delete(*args, **kwargs)
        user.publish()

    def set_as_primary(self, conditional=False):
        # shadow parent function, but use CachedEmailAddress manager to ensure cache gets updated
        old_primary = CachedEmailAddress.objects.get_primary(self.user)
        if old_primary:
            if conditional:
                return False
            old_primary.primary = False
            old_primary.save()
        return super(CachedEmailAddress, self).set_as_primary(conditional=conditional)

    def save(self, *args, **kwargs):
        super(CachedEmailAddress, self).save(*args, **kwargs)

        if not self.emailaddressvariant_set.exists() and self.email != self.email.lower():
            self.add_variant(self.email.lower())

#     @cachemodel.cached_method(auto_publish=True) # no caching due to errors in update_user_params
    def cached_variants(self):
        return self.emailaddressvariant_set.all()

    def add_variant(self, email_variation):
        existing_variants = EmailAddressVariant.objects.filter(
            canonical_email=self, email=email_variation
        )
        if email_variation not in [e.email for e in existing_variants.all()]:
            return EmailAddressVariant.objects.create(
                canonical_email=self, email=email_variation
            )
        else:
            raise ValidationError("Email variant {} already exists".format(email_variation))


class ProxyEmailConfirmation(EmailConfirmation):
    class Meta:
        proxy = True
        verbose_name = _("email confirmation")
        verbose_name_plural = _("email confirmations")


class EmailAddressVariant(models.Model):
    email = models.EmailField(blank=False)
    canonical_email = models.ForeignKey(CachedEmailAddress, on_delete=models.CASCADE, blank=False)

    def save(self, *args, **kwargs):
        self.is_valid(raise_exception=True)

        super(EmailAddressVariant, self).save(*args, **kwargs)
        self.canonical_email.save()

    def __unicode__(self):
        return self.email

    @property
    def verified(self):
        return self.canonical_email.verified

    def is_valid(self, raise_exception=False):
        def fail(message):
            if raise_exception:
                raise ValidationError(message)
            else:
                self.error = message
                return False

        if not self.canonical_email_id:
            try:
                self.canonical_email = CachedEmailAddress.cached.get(email=self.email)
            except CachedEmailAddress.DoesNotExist:
                fail("Canonical Email Address not found")

        if not self.canonical_email.email.lower() == self.email.lower():
            fail("New EmailAddressVariant does not match stored email address.")

        return True


class AdministrateOtherUsersMixin(object):
    """
    Base class to group all administrate functionality for users, purely for readability
    """
    def get_administrable_staff(self):
        """
        :return: all staff memberships related to the object where user is staff, except for user's own staff memeberships
        """
        admin_staff_memberships = self.get_staff(['administrate_users'])
        all_related_staff_memberships = []
        for staff in admin_staff_memberships:
            related_staffs = staff.object.staff_items
            all_related_staff_memberships.append([staff for staff in related_staffs if staff.user is not self])
        return all_related_staff_memberships


class UserCachedObjectGetterMixin(object):
    """
    Base class to group all cached object-getter functionality of user, purely for readability
    """
    @cachemodel.cached_method(auto_publish=True)
    def cached_institution_staff(self):
        return list(self.institutionstaff_set.all())

    @cachemodel.cached_method(auto_publish=True)
    def cached_faculty_staff(self):
        return list(self.facultystaff_set.all())

    @cachemodel.cached_method(auto_publish=True)
    def cached_issuer_staff(self):
        return list(self.issuerstaff_set.all())

    @cachemodel.cached_method(auto_publish=True)
    def cached_badgeclass_staff(self):
        return list(self.badgeclassstaff_set.all())

    def get_staff(self, permissions):
        """
        get user's staff memberships where user has all these permissions
        :param permission: list of strings
        :return: list of unique staff memberships
        """
        institution_staff = self.cached_institution_staff()
        faculty_staff = self.cached_faculty_staff()
        issuer_staff = self.cached_issuer_staff()
        badgeclass_staff = self.cached_badgeclass_staff()
        all_staffs = institution_staff + faculty_staff + issuer_staff + badgeclass_staff
        return [staff for staff in all_staffs if staff.has_permissions(permissions)]

    def get_faculties(self, permissions):
        """
        get faculties where user has all these permissions for
        :param permission: list of strings
        :return: list of faculties
        """
        institution_staff = self.cached_institution_staff()
        faculty_staff = self.cached_faculty_staff()
        staff_memberships = institution_staff+faculty_staff
        faculties = []
        for staff_membership in staff_memberships:
            if staff_membership.has_permissions(permissions):
                if staff_membership.__class__.__name__ is not 'FacultyStaff':
                    faculties += staff_membership.object.cached_faculties()
                else:
                    faculties += [staff_membership.object]
        return list(set(faculties))

    def get_issuers(self, permissions):
        """
        get issuers where user has all these permissions for
        :param permission: list of strings
        :return: list of issuers
        """
        institution_staff = self.cached_institution_staff()
        faculty_staff = self.cached_faculty_staff()
        issuer_staff = self.cached_issuer_staff()
        staff_memberships = institution_staff + faculty_staff + issuer_staff
        issuers = []
        for staff_membership in staff_memberships:
            if staff_membership.has_permissions(permissions):
                if staff_membership.__class__.__name__ is not 'IssuerStaff':
                    issuers += staff_membership.object.cached_issuers()
                else:
                    issuers += [staff_membership.object]
        return list(set(issuers))

    def get_badgeclasses(self, permissions):
        """
        get badgeclasses where user has all these permissions for
        :param permission: list of strings
        :return: list of badgeclasses
        """
        institution_staff = self.cached_institution_staff()
        faculty_staff = self.cached_faculty_staff()
        issuer_staff = self.cached_issuer_staff()
        badgeclass_staff = self.cached_badgeclass_staff()
        all_staff_memberships = institution_staff+faculty_staff+issuer_staff+badgeclass_staff
        badgeclasses = []
        for staff_membership in all_staff_memberships:
            if staff_membership.has_permissions(permissions):
                if staff_membership.__class__.__name__ is not 'BadgeClassStaff':
                    badgeclasses += staff_membership.object.cached_badgeclasses()
                else:
                    badgeclasses += [staff_membership.object]
        return list(set(badgeclasses))

    @cachemodel.cached_method(auto_publish=True)
    def cached_badgeinstances(self):
        return BadgeInstance.objects.filter(recipient_identifier=self.get_recipient_identifier())

    # @cachemodel.cached_method(auto_publish=True)
    # turned it off, because if user logs in for FIRST time, this caching will result in the user having no verified emails.
    # This results in api calls responding with a 403 after the failure of the AuthenticatedWithVerifiedEmail permission check.
    # Which will logout the user automatically with the error: Token expired.
    def cached_emails(self):
        return CachedEmailAddress.objects.filter(user=self)

    @cachemodel.cached_method(auto_publish=True)
    def cached_backpackcollections(self):
        return BackpackCollection.objects.filter(created_by=self)

    def cached_email_variants(self):
        return chain.from_iterable(email.cached_variants() for email in self.cached_emails())

    @cachemodel.cached_method(auto_publish=True)
    def cached_externaltools(self):
        return [a.cached_externaltool for a in self.externaltooluseractivation_set.filter(is_active=True)]

    @cachemodel.cached_method(auto_publish=True)
    def cached_token(self):
        user_token, created = \
                Token.objects.get_or_create(user=self)
        return user_token.key

    @cachemodel.cached_method(auto_publish=True)
    def cached_agreed_terms_version(self):
        try:
            return self.termsagreement_set.all().filter(valid=True).order_by('-terms_version')[0]
        except IndexError:
            pass
        return None


class UserPermissionsMixin(object):
    """
    Base class to group all permission functionality of user, purely for readability
    """
    def get_permissions(self, obj):
        """
        Convenience method to get permissions for this user & object combination
        :param obj: Instance of Institution, Faculty, Issuer or Badgeclass
        :return: dictionary
        """
        return obj.get_permissions(self)

    def gains_permission(self, permission_codename, model):
        content_type = ContentType.objects.get_for_model(model)
        permission = Permission.objects.get(codename=permission_codename, content_type=content_type)
        self.user_permissions.add(permission)
        # you still need to reload user from db to refresh permission cache if you want effect to be immediate

    def loses_permission(self, permission_codename, model):
        content_type = ContentType.objects.get_for_model(model)
        permission = Permission.objects.get(codename=permission_codename, content_type=content_type)
        self.user_permissions.remove(permission)
        # you still need to reload user from db to refresh permission cache if you want effect to be immediate

    @property
    def may_sign_assertions(self):
        return self.has_perm('signing.may_sign_assertions')

    @property
    def highest_group(self):
        groups = list(self.groups.filter(entity_rank__rank__gte=0))
        groups.sort(key=lambda x: x.entity_rank.rank)
        if groups:
            return groups[0]
        else:
            return None

    def may_enroll(self, badge_class):
        """
        Checks to see if user may enroll
            no enrollments: May enroll
            any not awarded assertions: May not enroll
            Any awarded and not revoked: May not enroll
            All revoked: May enroll
        """
        social_account = self.get_social_account()
        if social_account.provider == 'edu_id' or social_account.provider == 'surfconext_ala':
            enrollments = StudentsEnrolled.objects.filter(user=social_account.user, badge_class_id=badge_class.pk)
            if not enrollments:
                return True # no enrollments
            else:
                for enrollment in enrollments:
                    if not bool(enrollment.badge_instance): # has never been awarded
                        return False
                    else: #has been awarded
                        if not enrollment.assertion_is_revoked():
                            return False
                return True # all have been awarded and revoked
        else: # no eduID
            return False

    def staff_memberships(self):
        """
        Returns all staff memberships
        """
        return Issuer.objects.filter(staff__id=self.id)

    def within_scope(self, object):
        if object:
            if self.has_perm('badgeuser.has_institution_scope'):
                return object.institution == self.institution
            if self.has_perm('badgeuser.has_faculty_scope'):
                if object.faculty.__class__.__name__ == 'ManyRelatedManager':
                    return bool(set(object.faculty.all()).intersection(set(self.faculty.all())))
                else:
                    return object.faculty in self.faculty.all()
        return False


class BadgeUser(AdministrateOtherUsersMixin, UserCachedObjectGetterMixin, UserPermissionsMixin, BaseVersionedEntity, AbstractUser, cachemodel.CacheModel):
    """
    A full-featured user model that can be an Earner, Issuer, or Consumer of Open Badges
    """
    entity_class_name = 'BadgeUser'

    badgrapp = models.ForeignKey('mainsite.BadgrApp', on_delete=models.SET_NULL, blank=True, null=True, default=None)
    is_staff = models.BooleanField(
        _('Backend-staff member'),
        default=False,
        help_text=_('Designates whether the user can log into this admin site.'),
    )

    # canvas LTI id
    lti_id = models.CharField(unique=True, max_length=50, default=None, null=True, blank=True,
                              help_text='LTI user id, unique per user')
    marketing_opt_in = models.BooleanField(default=False)

    objects = BadgeUserManager()

    class Meta:
        verbose_name = _('badge user')
        verbose_name_plural = _('badge users')
        db_table = 'users'
        permissions=(('view_issuer_tab', 'User can view Issuer tab in front end'),
                     ('view_management_tab', 'User can view Management dashboard'),
                     ('has_faculty_scope', 'User has faculty scope'),
                     ('has_institution_scope', 'User has institution scope'),
                     ('ui_issuer_add', 'User can add issuer in front end'),
                     )

    def __unicode__(self):
        return "{} <{}>".format(self.get_full_name(), self.email)

    @property
    def institution(self):
        return self.institution_set.get()

    @institution.setter
    def institution(self, value):
        """
        :param value: Institution
        :return: None
        """
        self.institution_set.add(value)


    @property
    def email_items(self):
        return self.cached_emails()

    @email_items.setter
    def email_items(self, value):
        """
        Update this users EmailAddress from a list of BadgeUserEmailSerializerV2 data
        :param value: list(BadgeUserEmailSerializerV2)
        :return: None
        """
        if len(value) < 1:
            raise ValidationError("Must have at least 1 email")

        new_email_idx = {d['email']: d for d in value}

        primary_count = sum(1 if d.get('primary', False) else 0 for d in value)
        if primary_count != 1:
            raise ValidationError("Must have exactly 1 primary email")

        with transaction.atomic():
            # add or update existing items
            for email_data in value:
                primary = email_data.get('primary', False)
                emailaddress, created = CachedEmailAddress.cached.get_or_create(
                    email=email_data['email'],
                    defaults={
                        'user': self,
                        'primary': primary
                    })
                if created:
                    # new email address send a confirmation
                    emailaddress.send_confirmation()
                else:
                    if emailaddress.user_id == self.id:
                        # existing email address owned by user
                        emailaddress.primary = primary
                        emailaddress.save()
                    elif not emailaddress.verified:
                        # existing unverified email address, handover to this user
                        emailaddress.user = self
                        emailaddress.primary = primary
                        emailaddress.save()
                        emailaddress.send_confirmation()
                    else:
                        # existing email address used by someone else
                        raise ValidationError("Email '{}' may already be in use".format(email_data.get('email')))

            # remove old items
            for emailaddress in self.email_items:
                if emailaddress.email not in new_email_idx:
                    emailaddress.delete()

    @property
    def current_symmetric_key(self):
        return self.symmetrickey_set.get(current=True)

    def get_badgr_app(self):
        if self.badgrapp:
            return self.badgrapp
        else:
            return BadgrApp.objects.all().first()

    def get_full_name(self):
        return "%s %s" % (self.first_name, self.last_name)

    def email_user(self, subject, message, from_email=None, attachments=None, **kwargs):
        """
        Sends an email to this User.
        """
        try:
            EmailBlacklist.objects.get(email=self.primary_email)
        except EmailBlacklist.DoesNotExist:
            # Allow sending, as this email is not blacklisted.
            if not attachments:
                send_mail(subject, message, from_email, [self.primary_email], **kwargs)
            else:
                from django.core.mail import EmailMessage
                email = EmailMessage(subject=subject,
                                     body=message,
                                     from_email=from_email,
                                     to=[self.primary_email],
                                     attachments=attachments)
                email.send()
        else:
            return
            # TODO: Report email non-delivery somewhere.

    def publish(self):
        super(BadgeUser, self).publish()
        self.publish_by('username')

    def delete(self, *args, **kwargs):
        super(BadgeUser, self).delete(*args, **kwargs)
        self.publish_delete('username')

    def can_add_variant(self, email):
        try:
            canonical_email = CachedEmailAddress.objects.get(email=email, user=self, verified=True)
        except CachedEmailAddress.DoesNotExist:
            return False

        if email != canonical_email.email \
                and email not in [e.email for e in canonical_email.cached_variants()] \
                and EmailAddressVariant(email=email, canonical_email=canonical_email).is_valid():
            return True
        return False

    @property
    def primary_email(self):
        primaries = [e for e in self.cached_emails() if e.primary]
        if len(primaries) > 0:
            return primaries[0].email
        return self.email

    @property
    def verified_emails(self):
        return [e for e in self.cached_emails() if e.verified]

    @property
    def verified(self):
        if self.is_superuser:
            return True

        if len(self.verified_emails) > 0:
            return True

        return False

    @property
    def all_recipient_identifiers(self):
        return [self.get_recipient_identifier()]
#         return [e.email for e in self.cached_emails() if e.verified] + [e.email for e in self.cached_email_variants()]

    def get_recipient_identifier(self):
        from allauth.socialaccount.models import SocialAccount
        try:
            account = SocialAccount.objects.get(user=self.pk)
            return account.extra_data['sub']
        except SocialAccount.DoesNotExist:
            return None

    def get_social_account(self):
        from allauth.socialaccount.models import SocialAccount
        try:
            account = SocialAccount.objects.get(user=self.pk)
            return account
        except SocialAccount.DoesNotExist:
            return None

    def has_edu_id_social_account(self):
        social_account = self.get_social_account()
        return social_account.provider == 'edu_id' or social_account.provider == 'surfconext_ala'

    def has_surf_conext_social_account(self):
        social_account = self.get_social_account()
        return social_account.provider == 'surf_conext'

    def is_email_verified(self, email):
        if email in [e.email for e in self.verified_emails]:
            return True

        try:
            app_infos = ApplicationInfo.objects.filter(application__user=self)
            if any(app_info.trust_email_verification for app_info in app_infos):
                return True
        except ApplicationInfo.DoesNotExist:
            return False

        return False

    def get_assertions_ready_for_signing(self):
        assertion_timestamps = AssertionTimeStamp.objects.filter(signer=self).exclude(proof='')
        return [ts.badge_instance for ts in assertion_timestamps if ts.badge_instance.signature == None]

    @property
    def peers(self):
        """
        a BadgeUser is a Peer of another BadgeUser if they appear in an IssuerStaff together
        """
        # cached_issuers should become get_issuers
        # return set(chain(*[[s.cached_user for s in i.cached_issuerstaff()] for i in self.cached_issuers()]))
        raise NotImplementedError

    @property
    def agreed_terms_version(self):
        v = self.cached_agreed_terms_version()
        if v is None:
            return 0
        return v.terms_version

    @agreed_terms_version.setter
    def agreed_terms_version(self, value):
        try:
            value = int(value)
        except ValueError as e:
            return

        if value > self.agreed_terms_version:
            if TermsVersion.active_objects.filter(version=value).exists():
                if not self.pk:
                    self.save()
                self.termsagreement_set.get_or_create(terms_version=value, defaults=dict(agreed=True))


    def replace_token(self):
        Token.objects.filter(user=self).delete()
        # user_token, created = \
        #         Token.objects.get_or_create(user=self)
        self.save()
        return self.cached_token()


    def save(self, *args, **kwargs):
        if not self.username:
            # md5 hash the email and then encode as base64 to take up only 25 characters
            hashed = md5(self.email + ''.join(random.choice(string.lowercase) for i in range(64))).digest().encode('base64')[:-1]  # strip last character because its a newline
            self.username = "badgr{}".format(hashed[:25])

        if getattr(settings, 'BADGEUSER_SKIP_LAST_LOGIN_TIME', True):
            # skip saving last_login to the database
            if 'update_fields' in kwargs and kwargs['update_fields'] is not None and 'last_login' in kwargs['update_fields']:
                kwargs['update_fields'].remove('last_login')
                if len(kwargs['update_fields']) < 1:
                    # nothing to do, abort so we dont call .publish()
                    return
        return super(BadgeUser, self).save(*args, **kwargs)


class BadgeUserProxy(BadgeUser):
    class Meta:
        proxy = True
        verbose_name = 'Badge User Interface for SuperUser'


class BadgrAccessTokenManager(models.Manager):

    def generate_new_token_for_user(self, user, scope='r:profile', application=None, expires=None, refresh_token=False):
        with transaction.atomic():
            if application is None:
                application, created = Application.objects.get_or_create(
                    client_id='public',
                    client_type=Application.CLIENT_PUBLIC,
                    authorization_grant_type=Application.GRANT_PASSWORD,
                )
                if created:
                    ApplicationInfo.objects.create(application=application)

            if expires is None:
                access_token_expires_seconds = getattr(settings, 'OAUTH2_PROVIDER', {}).get('ACCESS_TOKEN_EXPIRE_SECONDS', 86400)
                expires = timezone.now() + datetime.timedelta(seconds=access_token_expires_seconds)

            accesstoken = self.create(
                application=application,
                user=user,
                expires=expires,
                token=generate_token(),
                scope=scope
            )

        return accesstoken

    def get_from_entity_id(self, entity_id):
        # lookup by a faked
        padding = len(entity_id) % 4
        if padding > 0:
            entity_id = '{}{}'.format(entity_id, (4-padding)*'=')
        decoded = base64.urlsafe_b64decode(entity_id.encode('utf-8'))
        id = re.sub(r'^{}'.format(self.model.fake_entity_id_prefix), '', decoded)
        try:
            pk = int(id)
        except ValueError as e:
            pass
        else:
            try:
                obj = self.get(pk=pk)
            except self.model.DoesNotExist:
                pass
            else:
                return obj
        raise self.model.DoesNotExist


class BadgrAccessToken(AccessToken, cachemodel.CacheModel):
    objects = BadgrAccessTokenManager()
    fake_entity_id_prefix = "BadgrAccessToken.id="

    class Meta:
        proxy = True

    @property
    def entity_id(self):
        # fake an entityId for this non-entity
        digest = "{}{}".format(self.fake_entity_id_prefix, self.pk)
        b64_string = base64.urlsafe_b64encode(digest)
        b64_trimmed = re.sub(r'=+$', '', b64_string)
        return b64_trimmed

    def get_entity_class_name(self):
        return 'AccessToken'

    @property
    def application_name(self):
        return self.application.name

    @property
    def applicationinfo(self):
        try:
            return self.application.applicationinfo
        except ApplicationInfo.DoesNotExist:
            return ApplicationInfo()


class TermsVersionManager(cachemodel.CacheModelManager):
    latest_version_key = "badgr_server_cached_latest_version"

    def latest_version(self):
        latest = self.cached_latest()
        if latest is not None:
            return latest.version
        return 0

    def latest(self):
        try:
            return self.filter(is_active=True).order_by('-version')[0]
        except IndexError:
            pass

    def cached_latest(self):
        latest = cache.get(self.latest_version_key)
        if latest is None:
            return self.publish_latest()
        return latest

    def publish_latest(self):
        latest = self.latest()
        if latest is not None:
            cache.set(self.latest_version_key, latest, timeout=None)
        return latest


class TermsVersion(IsActive, BaseAuditedModel, cachemodel.CacheModel):
    version = models.PositiveIntegerField(unique=True)
    short_description = models.TextField(blank=True)

    terms_and_conditions_template = models.CharField('Terms and conditions template',
                                                     null=True,
                                                     max_length=512
                                                     )
    accepted_terms_and_conditions_hash = models.CharField('Term and conditions hash',max_length=32,null=True)
    teacher = models.BooleanField(default=False)
    cached = TermsVersionManager()

    def publish(self):
        super(TermsVersion, self).publish()
        TermsVersion.cached.publish_latest()


class TermsAgreement(BaseAuditedModel, cachemodel.CacheModel):
    user = models.ForeignKey('badgeuser.BadgeUser', on_delete=models.CASCADE)
    terms_version = models.PositiveIntegerField()
    agreed = models.BooleanField(default=True)
    valid = models.BooleanField(default=True)

    class Meta:
        ordering = ('-terms_version',)
        unique_together = ('user', 'terms_version')


# Group.add_to_class('entity_id', models.CharField(max_length=254, null=True, default=None))
# Group.add_to_class('rank', models.PositiveIntegerField(null=True, default=None))
class GroupEntity(models.Model):
    group = models.OneToOneField(Group, on_delete=models.CASCADE, related_name='entity_rank')
    entity_id = models.CharField(max_length=254, null=True, default=None)
    rank = models.PositiveIntegerField(null=True, default=None)

    def __str__(self):
        return self.group.name

@receiver(post_save, sender=Group)
def generate_entity_id(sender, instance, **kwargs):
    try:
        instance.entity_rank
    except ObjectDoesNotExist:
        GroupEntity.objects.create(group=instance, entity_id=generate_entity_uri())

@receiver(pre_save, sender=GroupEntity)
def check_rank_uniqueness(sender, instance, **kwargs):
    if instance.rank is not None:
        instance_with_same_rank = sender.objects.filter(rank=instance.rank).first()
        if bool(instance_with_same_rank) and instance_with_same_rank != instance:
            raise ValidationError("Group rank already ascribed, choose another")
