"""Tests for django_bolt.nanodjango plugin (BoltAPI subclass and hooks)."""

from __future__ import annotations

import ast
import sys
import types
from importlib.metadata import entry_points
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings

from django_bolt.api import BoltAPI as RealBoltAPI
from django_bolt.nanodjango import (
    BoltAPI,
    convert_build_app_api,
    convert_build_settings,
    django_pre_setup,
)


@pytest.fixture(autouse=True)
def _preserve_settings_mutations():
    """Isolate test mutations to Django settings between tests.

    These tests mutate ``settings.INSTALLED_APPS`` and ``settings.BOLT_API``.
    Without restoration, leaked values (especially ``BOLT_API`` pointing to
    deleted fake modules) break unrelated tests like ``test_runbolt_dev``'s
    ``autodiscover_apis`` checks, which take an early-return path when
    ``BOLT_API`` is set.
    """
    original_apps = list(settings.INSTALLED_APPS)
    had_bolt_api = hasattr(settings, "BOLT_API")
    original_bolt_api = list(settings.BOLT_API) if had_bolt_api else None
    yield
    settings.INSTALLED_APPS = original_apps
    if had_bolt_api:
        settings.BOLT_API = original_bolt_api
    elif hasattr(settings, "BOLT_API"):
        del settings.BOLT_API


class TestBoltAPIInit:
    def test_adds_django_bolt_to_installed_apps(self):
        # Remove django_bolt if already present so we can test it gets added
        apps = [a for a in settings.INSTALLED_APPS if a != "django_bolt"]
        settings.INSTALLED_APPS = apps

        BoltAPI()
        assert "django_bolt" in settings.INSTALLED_APPS

    def test_no_duplicate_installed_apps(self):
        """Instantiating twice should not duplicate the entry."""
        settings.INSTALLED_APPS = list(settings.INSTALLED_APPS)
        BoltAPI()
        BoltAPI()
        count = settings.INSTALLED_APPS.count("django_bolt")
        assert count == 1

    def test_captures_calling_module_name(self):
        bolt = BoltAPI()
        assert bolt._module_name == __name__

    def test_bolt_api_not_configured_initially(self):
        bolt = BoltAPI()
        assert bolt._bolt_api_configured is False

    def test_isinstance_of_real_bolt_api(self):
        bolt = BoltAPI()
        assert isinstance(bolt, RealBoltAPI)


class TestConfigureBoltAPI:
    def test_sets_bolt_api_setting_on_first_route(self):
        # Create a fake module and register our BoltAPI instance in it
        mod = types.ModuleType("fake_module_for_test")
        bolt = BoltAPI()
        bolt._module_name = "fake_module_for_test"
        mod.my_bolt = bolt
        sys.modules["fake_module_for_test"] = mod

        try:
            # Clear any existing BOLT_API
            if hasattr(settings, "BOLT_API"):
                del settings.BOLT_API

            bolt._configure_bolt_api()

            assert bolt._bolt_api_configured is True
            assert "fake_module_for_test:my_bolt" in settings.BOLT_API
        finally:
            del sys.modules["fake_module_for_test"]

    def test_skips_if_already_configured(self):
        bolt = BoltAPI()
        bolt._bolt_api_configured = True

        # Should return immediately without touching settings
        if hasattr(settings, "BOLT_API"):
            del settings.BOLT_API

        bolt._configure_bolt_api()
        assert not hasattr(settings, "BOLT_API") or "BOLT_API" not in dir(settings)

    def test_skips_if_module_not_found(self):
        bolt = BoltAPI()
        bolt._module_name = "nonexistent_module_xyz"

        bolt._configure_bolt_api()
        assert bolt._bolt_api_configured is False

    def test_no_duplicate_bolt_api_entries(self):
        mod = types.ModuleType("fake_module_dedup")
        bolt = BoltAPI()
        bolt._module_name = "fake_module_dedup"
        mod.api = bolt
        sys.modules["fake_module_dedup"] = mod

        try:
            settings.BOLT_API = ["fake_module_dedup:api"]
            bolt._configure_bolt_api()
            count = settings.BOLT_API.count("fake_module_dedup:api")
            assert count == 1
        finally:
            del sys.modules["fake_module_dedup"]


class TestHTTPMethodDecorators:
    """Each HTTP method decorator should trigger _configure_bolt_api."""

    def _make_bolt_in_module(self):
        mod = types.ModuleType("fake_mod_http")
        bolt = BoltAPI()
        bolt._module_name = "fake_mod_http"
        mod.bolt = bolt
        sys.modules["fake_mod_http"] = mod
        return bolt, mod

    def _cleanup(self):
        sys.modules.pop("fake_mod_http", None)

    def test_get_triggers_configure(self):
        bolt, mod = self._make_bolt_in_module()
        try:
            assert bolt._bolt_api_configured is False

            @bolt.get("/test")
            async def handler(request):
                return {"ok": True}

            assert bolt._bolt_api_configured is True
        finally:
            self._cleanup()

    def test_post_triggers_configure(self):
        bolt, mod = self._make_bolt_in_module()
        try:

            @bolt.post("/test")
            async def handler(request):
                return {"ok": True}

            assert bolt._bolt_api_configured is True
        finally:
            self._cleanup()

    def test_put_triggers_configure(self):
        bolt, mod = self._make_bolt_in_module()
        try:

            @bolt.put("/test")
            async def handler(request):
                return {"ok": True}

            assert bolt._bolt_api_configured is True
        finally:
            self._cleanup()

    def test_patch_triggers_configure(self):
        bolt, mod = self._make_bolt_in_module()
        try:

            @bolt.patch("/test")
            async def handler(request):
                return {"ok": True}

            assert bolt._bolt_api_configured is True
        finally:
            self._cleanup()

    def test_delete_triggers_configure(self):
        bolt, mod = self._make_bolt_in_module()
        try:

            @bolt.delete("/test")
            async def handler(request):
                return {"ok": True}

            assert bolt._bolt_api_configured is True
        finally:
            self._cleanup()

    def test_head_triggers_configure(self):
        bolt, mod = self._make_bolt_in_module()
        try:

            @bolt.head("/test")
            async def handler(request):
                return {"ok": True}

            assert bolt._bolt_api_configured is True
        finally:
            self._cleanup()

    def test_options_triggers_configure(self):
        bolt, mod = self._make_bolt_in_module()
        try:

            @bolt.options("/test")
            async def handler(request):
                return {"ok": True}

            assert bolt._bolt_api_configured is True
        finally:
            self._cleanup()

    def test_websocket_triggers_configure(self):
        bolt, mod = self._make_bolt_in_module()
        try:

            @bolt.websocket("/test")
            async def handler(request):
                pass

            assert bolt._bolt_api_configured is True
        finally:
            self._cleanup()


class TestDjangoPreSetup:
    def test_adds_django_bolt_when_installed(self):
        apps = [a for a in settings.INSTALLED_APPS if a != "django_bolt"]
        settings.INSTALLED_APPS = apps

        with patch("django_bolt.nanodjango.defer") as mock_defer:
            mock_defer.is_installed.return_value = True
            django_pre_setup(app=MagicMock())

        assert "django_bolt" in settings.INSTALLED_APPS

    def test_skips_when_not_installed(self):
        apps = [a for a in settings.INSTALLED_APPS if a != "django_bolt"]
        settings.INSTALLED_APPS = apps

        with patch("django_bolt.nanodjango.defer") as mock_defer:
            mock_defer.is_installed.return_value = False
            django_pre_setup(app=MagicMock())

        assert "django_bolt" not in settings.INSTALLED_APPS

    def test_no_duplicate_when_already_present(self):
        if "django_bolt" not in settings.INSTALLED_APPS:
            settings.INSTALLED_APPS = list(settings.INSTALLED_APPS) + ["django_bolt"]

        with patch("django_bolt.nanodjango.defer") as mock_defer:
            mock_defer.is_installed.return_value = True
            django_pre_setup(app=MagicMock())

        count = list(settings.INSTALLED_APPS).count("django_bolt")
        assert count == 1


class TestConvertBuildSettings:
    def test_appends_django_bolt_to_installed_apps(self):
        source = "INSTALLED_APPS = ['django.contrib.contenttypes', 'django.contrib.auth']"
        settings_ast = ast.parse(source)

        convert_build_settings(MagicMock(), MagicMock(), settings_ast)

        # Inspect the resulting AST
        assign = settings_ast.body[0]
        elts = assign.value.elts
        values = [e.value for e in elts]
        assert "django_bolt" in values

    def test_skips_if_no_installed_apps_assignment(self):
        source = "DEBUG = True"
        settings_ast = ast.parse(source)

        # Should not raise
        convert_build_settings(MagicMock(), MagicMock(), settings_ast)


def _make_converter(source: str):
    """Build a mock converter from Python source code."""
    tree = ast.parse(source)
    converter = MagicMock()
    converter.ast = tree
    return converter


def _make_resolver():
    resolver = MagicMock()
    resolver.add_object = MagicMock()
    resolver.add_references = MagicMock()
    return resolver


class TestConvertBuildAppApi:
    def test_detects_boltapi_assignment(self):
        source = "bolt = BoltAPI()"
        converter = _make_converter(source)
        resolver = _make_resolver()
        extra_src = []

        result_resolver, result_src = convert_build_app_api(
            converter, resolver, extra_src
        )

        resolver.add_object.assert_any_call("bolt")
        assert len(result_src) == 1
        assert "BoltAPI" in result_src[0]

    def test_detects_attribute_boltapi(self):
        """Detect bolt = django_bolt.nanodjango.BoltAPI()"""
        source = "bolt = django_bolt.nanodjango.BoltAPI()"
        converter = _make_converter(source)
        resolver = _make_resolver()
        extra_src = []

        convert_build_app_api(converter, resolver, extra_src)

        resolver.add_object.assert_any_call("bolt")
        assert len(extra_src) == 1

    def test_detects_decorated_async_function(self):
        source = """\
bolt = BoltAPI()

@bolt.get("/hello")
async def hello(request):
    return {"message": "hello"}
"""
        converter = _make_converter(source)
        resolver = _make_resolver()
        extra_src = []

        convert_build_app_api(converter, resolver, extra_src)

        resolver.add_object.assert_any_call("bolt")
        resolver.add_object.assert_any_call("hello")
        assert len(extra_src) == 2

    def test_detects_decorated_sync_function(self):
        source = """\
bolt = BoltAPI()

@bolt.post("/items")
def create_item(request):
    return {"id": 1}
"""
        converter = _make_converter(source)
        resolver = _make_resolver()
        extra_src = []

        convert_build_app_api(converter, resolver, extra_src)

        resolver.add_object.assert_any_call("create_item")
        assert len(extra_src) == 2

    def test_detects_all_http_methods(self):
        methods = ["get", "post", "put", "patch", "delete", "head", "options", "websocket"]
        for method in methods:
            source = f"""\
bolt = BoltAPI()

@bolt.{method}("/test")
async def handler(request):
    pass
"""
            converter = _make_converter(source)
            resolver = _make_resolver()
            extra_src = []

            convert_build_app_api(converter, resolver, extra_src)

            resolver.add_object.assert_any_call("handler")
            assert len(extra_src) == 2, f"Failed for method: {method}"

    def test_ignores_non_bolt_functions(self):
        source = """\
bolt = BoltAPI()

def regular_function():
    pass

@some_other_decorator
def other_function():
    pass
"""
        converter = _make_converter(source)
        resolver = _make_resolver()
        extra_src = []

        convert_build_app_api(converter, resolver, extra_src)

        assert len(extra_src) == 1
        assert "BoltAPI" in extra_src[0]

    def test_ignores_unknown_method_on_bolt(self):
        """@bolt.trace or other unknown methods should not be detected."""
        source = """\
bolt = BoltAPI()

@bolt.trace("/test")
async def handler(request):
    pass
"""
        converter = _make_converter(source)
        resolver = _make_resolver()
        extra_src = []

        convert_build_app_api(converter, resolver, extra_src)

        assert len(extra_src) == 1

    def test_multiple_api_instances(self):
        source = """\
api1 = BoltAPI()
api2 = BoltAPI()

@api1.get("/a")
async def route_a(request):
    pass

@api2.post("/b")
async def route_b(request):
    pass
"""
        converter = _make_converter(source)
        resolver = _make_resolver()
        extra_src = []

        convert_build_app_api(converter, resolver, extra_src)

        resolver.add_object.assert_any_call("api1")
        resolver.add_object.assert_any_call("api2")
        resolver.add_object.assert_any_call("route_a")
        resolver.add_object.assert_any_call("route_b")
        assert len(extra_src) == 4

    def test_empty_module(self):
        converter = _make_converter("")
        resolver = _make_resolver()
        extra_src = []

        result_resolver, result_src = convert_build_app_api(
            converter, resolver, extra_src
        )

        resolver.add_object.assert_not_called()
        assert len(result_src) == 0

    def test_collects_references(self):
        source = """\
bolt = BoltAPI()

@bolt.get("/hello")
async def hello(request):
    return some_helper()
"""
        converter = _make_converter(source)
        resolver = _make_resolver()
        extra_src = []

        convert_build_app_api(converter, resolver, extra_src)

        assert resolver.add_references.call_count == 2

    def test_called_decorator_with_kwargs(self):
        """@bolt.get("/path", auth=True) style decorators should still work."""
        source = """\
bolt = BoltAPI()

@bolt.get("/hello", auth=True)
async def hello(request):
    pass
"""
        converter = _make_converter(source)
        resolver = _make_resolver()
        extra_src = []

        convert_build_app_api(converter, resolver, extra_src)

        resolver.add_object.assert_any_call("hello")
        assert len(extra_src) == 2

    def test_detects_mount_django(self):
        source = """\
bolt = BoltAPI()

@bolt.get("/api/hello")
async def hello(request):
    return {"message": "hello"}

bolt.mount_django("/")
"""
        converter = _make_converter(source)
        resolver = _make_resolver()
        extra_src = []

        convert_build_app_api(converter, resolver, extra_src)

        assert len(extra_src) == 3
        assert any("mount_django" in s for s in extra_src)

    def test_detects_mount(self):
        source = """\
bolt = BoltAPI()
other_api = BoltAPI()

bolt.mount("/other", other_api)
"""
        converter = _make_converter(source)
        resolver = _make_resolver()
        extra_src = []

        convert_build_app_api(converter, resolver, extra_src)

        assert len(extra_src) == 3
        assert any("mount" in s for s in extra_src)

    def test_ignores_mount_on_non_bolt_obj(self):
        """mount_django on a non-BoltAPI object should not be detected."""
        source = """\
bolt = BoltAPI()
other = SomeOtherClass()

other.mount_django("/")
"""
        converter = _make_converter(source)
        resolver = _make_resolver()
        extra_src = []

        convert_build_app_api(converter, resolver, extra_src)

        assert len(extra_src) == 1


class TestEntryPointWiring:
    """Verify nanodjango discovers our plugin via the setuptools entry point."""

    def test_entry_point_is_registered(self):
        """The `django-bolt` entry point under group `nanodjango` must resolve to
        `django_bolt.nanodjango`. This catches pyproject.toml misconfigurations."""
        eps = entry_points(group="nanodjango")
        bolt_eps = [ep for ep in eps if ep.name == "django-bolt"]
        assert len(bolt_eps) >= 1, f"expected 'django-bolt' entry point, got: {list(eps)}"
        assert bolt_eps[0].value == "django_bolt.nanodjango"

    def test_plugin_exposes_all_three_hooks(self):
        """Loading the entry point must yield a module exposing the three
        @hookimpl functions under their expected names."""
        ep = next(ep for ep in entry_points(group="nanodjango") if ep.name == "django-bolt")
        module = ep.load()

        for hook_name in ("django_pre_setup", "convert_build_settings", "convert_build_app_api"):
            assert hasattr(module, hook_name), (
                f"loaded plugin module missing {hook_name}"
            )
            assert callable(getattr(module, hook_name))
