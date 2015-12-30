# -*- coding: utf-8 -*-
"""django_jwt models."""
from django.contrib.auth.models import User
from django.db import models, transaction
from django.utils.timezone import now as tz_now

from jwt.exceptions import InvalidAudienceError

from django_jwt.exceptions import MaxUseError
from django_jwt.utils import to_seconds, encode


class RequestTokenQuerySet(models.query.QuerySet):

    """Custom QuerySet for RquestToken objects."""

    def create_token(self, scope, **kwargs):
        """Create a new RequestToken."""
        return RequestToken(scope=scope, **kwargs).save()


class RequestToken(models.Model):

    """A link token, targeted for use by a known Django User.

    A RequestToken contains information that can be encoded as a JWT
    (JSON Web Token). It is designed to be used in conjunction with the
    RequestTokenMiddleware (responsible for JWT verification) and the
    @use_request_token decorator (responsible for validating the token
    and setting the request.user correctly).

    Each token must have a 'scope', which is used to tie it to a view function
    that is decorated with the `use_request_token` decorator. The token can
    only be used by functions with matching scopes.

    The token may be set to a specific User, in which case, if the existing
    request is unauthenticated, it will use that user as the `request.user`
    property, allowing access to authenticated views.

    The token may be timebound by the `not_before_time` and `expiration_time`
    properties, which are registered JWT 'claims'.

    The token may be restricted by the number of times it can be used, through
    the `max_use` property, which is incremented each time it's used (NB *not*
    thread-safe).

    The token may also store arbitrary serializable data, which can be used
    by the view function if the request token is valid.

    JWT spec: https://tools.ietf.org/html/rfc7519

    """

    user = models.ForeignKey(
        User,
        blank=True, null=True,
        help_text="Intended recipient of the JWT (can be used by anyone if not set)."
    )
    scope = models.CharField(
        max_length=100,
        help_text="Label used to match request to view function in decorator."
    )
    expiration_time = models.DateTimeField(
        blank=True, null=True,
        help_text="Token will expire at this time (raises ExpiredSignatureError)."
    )
    not_before_time = models.DateTimeField(
        blank=True, null=True,
        help_text="Token cannot be used before this time (raises ImmatureSignatureError)."
    )
    data = models.TextField(
        max_length=1000,
        help_text="Custom data add to the token, but not encoded (must be fetched from DB).",
        blank=True,
        default='{}'
    )
    issued_at = models.DateTimeField(
        blank=True, null=True,
        help_text="Time the token was created (set in the initial save)."
    )
    max_uses = models.IntegerField(
        default=1,
        help_text="The maximum number of times the token can be used."
    )
    used_to_date = models.IntegerField(
        default=0,
        help_text="Number of times the token has been used to date (raises MaxUseError)."
    )

    objects = RequestTokenQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if 'update_fields' not in kwargs:
            self.issued_at = self.issued_at or tz_now()
        super(RequestToken, self).save(*args, **kwargs)
        return self

    @property
    def aud(self):
        """The 'aud' claim, maps to user.id."""
        return self.claims.get('aud')

    @property
    def exp(self):
        """The 'exp' claim, maps to expiration_time."""
        return self.claims.get('exp')

    @property
    def nbf(self):
        """The 'nbf' claim, maps to not_before_time."""
        return self.claims.get('nbf')

    @property
    def iat(self):
        """The 'iat' claim, maps to issued_at."""
        return self.claims.get('iat')

    @property
    def jti(self):
        """The 'jti' claim, maps to id."""
        return self.claims.get('jti')

    @property
    def max(self):
        """The 'max' claim, maps to max_uses."""
        return self.claims.get('max')

    @property
    def sub(self):
        """The 'sub' claim, maps to scope."""
        return self.claims.get('sub')

    @property
    def claims(self):
        """A dict containing all of the DEFAULT_CLAIMS (where values exist)."""
        claims = {
            'max': self.max_uses,
            'sub': self.scope
        }
        if self.id is not None:
            claims['jti'] = self.id
        if self.user is not None:
            claims['aud'] = self.user.id
        if self.expiration_time is not None:
            claims['exp'] = to_seconds(self.expiration_time)
        if self.issued_at is not None:
            claims['iat'] = to_seconds(self.issued_at)
        if self.not_before_time is not None:
            claims['nbf'] = to_seconds(self.not_before_time)
        return claims

    def jwt(self):
        """Encode the token claims into a JWT."""
        return encode(self.claims)

    def validate_request(self, request):
        """Validate token against the incoming request.

        This method checks the current token hasn't exceeded
        the usage count, that the request is valid (target_url, user).

        It may raise any of the following errors:

            # TargetUrlError
            MaxUseError
            InvalidAudienceError

        """
        self._validate_max_uses()
        self._validate_request_user(request.user)

    def _validate_max_uses(self):
        """Check that we haven't exceeded the max_uses value.

        Raise MaxUseError if we have overshot the value.

        """
        if self.used_to_date >= self.max_uses:
            raise MaxUseError("RequestToken [%s] has exceeded max uses" % self.id)

    def _validate_request_user(self, user):
        """Validate and set the request.user from object user.

        If the object has a user set, then it must match the request.user,
        and if it doesn't we raise an InvalidAudienceError.

        """
        # we have an authenticated user that does *not* match the user
        # we are expecting, so bomb out here.
        if user.is_authenticated() and user != self.user:
            raise InvalidAudienceError(
                "RequestToken [%s] audience mismatch: '%s' != '%s'" % (
                    self.id, user, self.user
                )
            )

    @transaction.atomic
    def log(self, request, response):
        """Record the use of a token.

        This is used by the decorator to log each time someone uses the token,
        or tries to. Used for reporting, diagnostics.

        Args:
            request: the HttpRequest object that used the token, from which the
                user, ip and user-agenct are extracted.
            response: the corresponding HttpResponse object, from which the status
                code is extracted.
            duration: float, the duration of the view function in ms - just because
                you can never measure too many things.

        Returns a RequestTokenUse object.

        """
        assert hasattr(request, 'user'), (
            "Request is missing user property. Please ensure that the Django "
            "authentication middleware is installed."
        )
        meta = request.META
        xff = meta.get('HTTP_X_FORWARDED_FOR', None)
        client_ip = xff or meta.get('REMOTE_ADDR', 'unknown')
        user = None if request.user.is_anonymous() else request.user
        rtu = RequestTokenLog(
            token=self,
            user=user,
            user_agent=meta.get('HTTP_USER_AGENT', 'unknown'),
            client_ip=client_ip,
            status_code=response.status_code
        ).save()
        # NB this could already be out-of-date
        self.used_to_date = models.F('used_to_date') + 1
        self.save()
        return rtu


class RequestTokenLog(models.Model):

    """Used to log the use of a RequestToken."""

    token = models.ForeignKey(
        RequestToken,
        help_text="The RequestToken that was used.",
        db_index=True
    )
    user = models.ForeignKey(
        User,
        blank=True, null=True,
        help_text="The user who made the request (None if anonymous)."
    )
    user_agent = models.TextField(
        blank=True,
        help_text="User-agent of client used to make the request."
    )
    client_ip = models.CharField(
        max_length=15,
        help_text="Client IP of device used to make the request."
    )
    status_code = models.IntegerField(
        blank=True, null=True,
        help_text="Response status code associated with this use of the token."
    )
    timestamp = models.DateTimeField(
        blank=True,
        help_text="Time the request was logged."
    )

    def save(self, *args, **kwargs):
        if 'update_fields' not in kwargs:
            self.timestamp = self.timestamp or tz_now()
        super(RequestTokenLog, self).save(*args, **kwargs)
        return self
