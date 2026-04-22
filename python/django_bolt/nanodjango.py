"""
django-bolt plugin for nanodjango single-file apps.

Provides a BoltAPI subclass that auto-configures Django settings so you can
write a production-ready single-file app without manual settings wrangling:

    from nanodjango import Django
    from django_bolt.nanodjango import BoltAPI

    app = Django()
    bolt = BoltAPI()

    @bolt.get('/hello')
    async def hello(request):
        return {'message': 'hello'}

Run with: python myapp.py runbolt --port 8001

Install: pip install "django-bolt[nanodjango]"
"""

from __future__ import annotations

import ast
import inspect
import sys
from typing import Any


from nanodjango import Django, defer, hookimpl
from nanodjango.convert.converter import Converter, Resolver
from nanodjango.convert.utils import collect_references, get_decorators

from .api import BoltAPI as _RealBoltAPI

_HTTP_METHODS: tuple[str, ...] = (
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "websocket",
)


class BoltAPI(_RealBoltAPI):
    """
    BoltAPI subclass for nanodjango single-file apps.

    Must be instantiated after ``app = Django()``, which is where
    settings.configure() is called (same requirement as the real BoltAPI).

    Auto-configures:
    - ``django_bolt`` added to ``INSTALLED_APPS``
    - ``settings.BOLT_API`` set to ``["<module>:<varname>"]`` on first route

    This subclass passes ``isinstance(bolt, django_bolt.api.BoltAPI)`` so
    django-bolt's ``runbolt`` autodiscovery finds it correctly.
    """

    def __init__(self, **kwargs: Any) -> None:
        # Capture calling module name before super().__init__ changes the frame
        frame = inspect.currentframe()
        self._module_name: str = frame.f_back.f_globals.get("__name__", "__main__")
        self._bolt_api_configured: bool = False

        super().__init__(**kwargs)

        # Add django_bolt to INSTALLED_APPS immediately on instantiation
        from django.conf import settings # noqa: 402

        if "django_bolt" not in settings.INSTALLED_APPS:
            settings.INSTALLED_APPS = list(settings.INSTALLED_APPS) + [
                "django_bolt"
            ]

    def _configure_bolt_api(self) -> None:
        """
        Scan the calling module's globals to find our variable name, then
        set settings.BOLT_API so runbolt autodiscovery can find this instance.

        Called lazily on the first route decorator so the variable is guaranteed
        to be in the module namespace by then.
        """
        if self._bolt_api_configured:
            return

        from django.conf import settings # noqa: 402

        module = sys.modules.get(self._module_name)
        if module is None:
            return

        for name, val in vars(module).items():
            if val is self:
                entry = f"{self._module_name}:{name}"
                existing = list(getattr(settings, "BOLT_API", []))
                if entry not in existing:
                    existing.append(entry)
                    settings.BOLT_API = existing
                self._bolt_api_configured = True
                return

    # Override each HTTP method to configure BOLT_API before registering routes
    def get(self, path: str, **kwargs: Any) -> Any:
        self._configure_bolt_api()
        return super().get(path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        self._configure_bolt_api()
        return super().post(path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        self._configure_bolt_api()
        return super().put(path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        self._configure_bolt_api()
        return super().patch(path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        self._configure_bolt_api()
        return super().delete(path, **kwargs)

    def head(self, path: str, **kwargs: Any) -> Any:
        self._configure_bolt_api()
        return super().head(path, **kwargs)

    def options(self, path: str, **kwargs: Any) -> Any:
        self._configure_bolt_api()
        return super().options(path, **kwargs)

    def websocket(self, path: str, **kwargs: Any) -> Any:
        self._configure_bolt_api()
        return super().websocket(path, **kwargs)


# ---------------------------------------------------------------------------
# Plugin hooks - auto-loaded by nanodjango via setuptools entry point
# ---------------------------------------------------------------------------


@hookimpl
def django_pre_setup(app: Django) -> None:
    """
    Add django_bolt to INSTALLED_APPS when the package is installed.

    This covers the edge case where django_bolt is installed but BoltAPI()
    is never instantiated (e.g. the app only uses runbolt management command
    without registering routes via this wrapper).
    """
    from django.conf import settings # noqa: 402

    if not defer.is_installed("django_bolt"):
        return

    if "django_bolt" not in settings.INSTALLED_APPS:
        settings.INSTALLED_APPS = list(settings.INSTALLED_APPS) + ["django_bolt"]


@hookimpl
def convert_build_settings(
    converter: Converter, resolver: Resolver, settings_ast: ast.AST
) -> None:
    """
    Add ``django_bolt`` to INSTALLED_APPS in the generated settings.py.
    """
    for node in settings_ast.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "INSTALLED_APPS"
                for t in node.targets
            )
            and isinstance(node.value, ast.List)
        ):
            node.value.elts.append(ast.Constant(value="django_bolt"))
            break


@hookimpl
def convert_build_app_api(
    converter: Converter, resolver: Resolver, extra_src: list[str]
) -> tuple[Resolver, list[str]]:
    """
    During ``nanodjango convert``, move BoltAPI instances and their route
    handlers into ``app/api.py``.

    Detects:
    - ``bolt = BoltAPI(...)``  (from django_bolt.nanodjango or django_bolt)
    - ``bolt = django_bolt.nanodjango.BoltAPI(...)``
    - ``@bolt.get(...)``, ``@bolt.post(...)``, etc. on async/sync functions
    - ``bolt.mount_django(...)`` and ``bolt.mount(...)`` calls
    """
    api_objs: set[str] = set()

    for obj_ast in converter.ast.body:
        is_bolt: bool = False

        # Detect: bolt = BoltAPI(...) or bolt = something.BoltAPI(...)
        if isinstance(obj_ast, ast.Assign) and isinstance(obj_ast.value, ast.Call):
            func: ast.expr = obj_ast.value.func
            func_name: str | None = None
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr

            if func_name == "BoltAPI":
                for target in obj_ast.targets:
                    if isinstance(target, ast.Name):
                        api_objs.add(target.id)
                        resolver.add_object(target.id)
                is_bolt = True

        # Detect: @bolt.get/post/... on async or sync function defs
        elif isinstance(obj_ast, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decorators: list[ast.expr] = get_decorators(obj_ast)
            for decorator in decorators:
                if isinstance(decorator, ast.Call):
                    decorator = decorator.func

                if (
                    isinstance(decorator, ast.Attribute)
                    and isinstance(decorator.value, ast.Name)
                    and decorator.value.id in api_objs
                    and decorator.attr in _HTTP_METHODS
                ):
                    resolver.add_object(obj_ast.name)
                    is_bolt = True
                    break

        # Detect: bolt.mount_django(...) or bolt.mount(...)
        elif isinstance(obj_ast, ast.Expr) and isinstance(obj_ast.value, ast.Call):
            func = obj_ast.value.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in api_objs
                and func.attr in ("mount_django", "mount")
            ):
                is_bolt = True

        if is_bolt:
            src: str = ast.unparse(obj_ast)
            references: set[str] = collect_references(obj_ast)
            resolver.add_references(references)
            extra_src.append(src)

    return resolver, extra_src
