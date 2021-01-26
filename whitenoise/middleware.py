import logging
import os
from itertools import chain
from posixpath import basename
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage, ManifestStaticFilesStorage
from django.contrib.staticfiles import finders
from django.http import FileResponse
from django.urls import get_script_prefix
from django.utils.functional import cached_property

from .base import WhiteNoise
from .string_utils import decode_if_byte_string, ensure_leading_trailing_slash


__all__ = [
    "WhiteNoiseMiddleware",
    "LazyWhiteNoiseMiddleware",
]


logger = logging.getLogger(__name__)


class WhiteNoiseFileResponse(FileResponse):
    """
    Wrap Django's FileResponse to prevent setting any default headers. For the
    most part these just duplicate work already done by WhiteNoise but in some
    cases (e.g. the content-disposition header introduced in Django 3.0) they
    are actively harmful.
    """

    def set_headers(self, *args, **kwargs):
        pass


class WhiteNoiseMiddleware(WhiteNoise):
    """
    Wrap WhiteNoise to allow it to function as Django middleware, rather
    than WSGI middleware

    This functions as both old- and new-style middleware, so can be included in
    either MIDDLEWARE or MIDDLEWARE_CLASSES.
    """

    config_attrs = WhiteNoise.config_attrs + ("root", "use_finders", "static_prefix")
    root = None
    use_finders = False
    static_prefix = None
    add_static_root_files = True

    def __init__(self, get_response=None, settings=settings):
        self.get_response = get_response
        self.configure_from_settings(settings)
        # Pass None for `application`
        super(WhiteNoiseMiddleware, self).__init__(None)
        if self.static_root:
            if self.add_static_root_files:
                self.add_files(self.static_root, prefix=self.static_prefix)
            else:
                self.insert_directory(self.static_root, self.static_prefix)
        if self.root:
            self.add_files(self.root)
        if self.use_finders and not self.autorefresh:
            self.add_files_from_finders()

    def can_lazy_load_url(self, url):
        return False

    def __call__(self, request):
        response = self.process_request(request)
        if response is None:
            response = self.get_response(request)
        return response

    def process_request(self, request):
        if self.autorefresh:
            static_file = self.find_file(request.path_info)
        else:
            static_file = self.files.get(request.path_info)
            if static_file is None and self.can_lazy_load_url(request.path_info):
                static_file = self.find_file(request.path_info)
                if static_file is not None:
                    logger.info('Lazy loaded %s', request.path_info)
                    self.files[request.path_info] = static_file
        if static_file is not None:
            return self.serve(static_file, request)

    @staticmethod
    def serve(static_file, request):
        response = static_file.get_response(request.method, request.META)
        status = int(response.status)
        http_response = WhiteNoiseFileResponse(response.file or (), status=status)
        # Remove default content-type
        del http_response["content-type"]
        for key, value in response.headers:
            http_response[key] = value
        return http_response

    def configure_from_settings(self, settings):
        # Default configuration
        self.autorefresh = settings.DEBUG
        self.use_finders = settings.DEBUG
        self.static_prefix = urlparse(settings.STATIC_URL or "").path
        script_prefix = get_script_prefix().rstrip("/")
        if script_prefix:
            if self.static_prefix.startswith(script_prefix):
                self.static_prefix = self.static_prefix[len(script_prefix) :]
        if settings.DEBUG:
            self.max_age = 0
        # Allow settings to override default attributes
        for attr in self.config_attrs:
            settings_key = "WHITENOISE_{0}".format(attr.upper())
            try:
                value = getattr(settings, settings_key)
            except AttributeError:
                pass
            else:
                value = decode_if_byte_string(value)
                setattr(self, attr, value)
        self.static_prefix = ensure_leading_trailing_slash(self.static_prefix)
        self.static_root = decode_if_byte_string(settings.STATIC_ROOT)

    def add_files_from_finders(self):
        files = {}
        for finder in finders.get_finders():
            for path, storage in finder.list(None):
                prefix = (getattr(storage, "prefix", None) or "").strip("/")
                url = u"".join(
                    (
                        self.static_prefix,
                        prefix,
                        "/" if prefix else "",
                        path.replace("\\", "/"),
                    )
                )
                # Use setdefault as only first matching file should be used
                files.setdefault(url, storage.path(path))
        stat_cache = {path: os.stat(path) for path in files.values()}
        for url, path in files.items():
            self.add_file_to_dictionary(url, path, stat_cache=stat_cache)

    def candidate_paths_for_url(self, url):
        if self.use_finders and url.startswith(self.static_prefix):
            path = finders.find(url[len(self.static_prefix) :])
            if path:
                yield path
        paths = super(WhiteNoiseMiddleware, self).candidate_paths_for_url(url)
        for path in paths:
            yield path

    def immutable_file_test(self, path, url):
        """
        Determine whether given URL represents an immutable file (i.e. a
        file with a hash of its contents as part of its name) which can
        therefore be cached forever
        """
        if not url.startswith(self.static_prefix):
            return False
        name = url[len(self.static_prefix) :]
        name_without_hash = self.get_name_without_hash(name)
        if name == name_without_hash:
            return False
        static_url = self.get_static_url(name_without_hash)
        # If the static_url function maps the name without hash
        # back to the original name, then we know we've got a
        # versioned filename
        if static_url and basename(static_url) == basename(url):
            return True
        return False

    def get_name_without_hash(self, filename):
        """
        Removes the version hash from a filename e.g, transforms
        'css/application.f3ea4bcc2.css' into 'css/application.css'

        Note: this is specific to the naming scheme used by Django's
        CachedStaticFilesStorage. You may have to override this if
        you are using a different static files versioning system
        """
        name_with_hash, ext = os.path.splitext(filename)
        name = os.path.splitext(name_with_hash)[0]
        return name + ext

    def get_static_url(self, name):
        try:
            return decode_if_byte_string(staticfiles_storage.url(name))
        except ValueError:
            return None


class LazyWhiteNoiseMiddleware(WhiteNoiseMiddleware):
    add_static_root_files = False

    @cached_property
    def known_static_urls(self):
        if isinstance(staticfiles_storage, ManifestStaticFilesStorage):
            serve_unhashed = not getattr(settings, "WHITENOISE_KEEP_ONLY_HASHED_FILES", False)
            return set(['{}{}'.format(self.static_prefix, n) for n in chain(
                staticfiles_storage.hashed_files.values(),
                staticfiles_storage.hashed_files.keys() if serve_unhashed else [],
            )])

    def can_lazy_load_url(self, url):
        return url.startswith(self.static_prefix) and \
            self.known_static_urls is None or url in self.known_static_urls
