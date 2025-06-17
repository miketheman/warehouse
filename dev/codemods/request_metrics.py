#!/usr/bin/env -S uv run --script --python 3.13
# /// script
# dependencies = [
#   "libcst",
# ]
# ///

# Run from Repo root with:
# uv run python3 -m libcst.tool codemod -x dev.codemods.request_metrics.ConvertMetricsCommand warehouse/

from typing import Union

import libcst as cst
from libcst.codemod import CodemodContext, VisitorBasedCodemodCommand


class ConvertMetricsCommand(VisitorBasedCodemodCommand):
    """
    Converts in-module usage of metrics to the `request.metrics` helper instead.

    The codemod:
    1. Finds imports of `IMetricsService` from `warehouse.metrics`
    2. Identifies variables assigned to `request.find_service(IMetricsService, ...)`
    3. Replaces method calls on those variables with `request.metrics.method(...)`
    4. Removes the import and assignment statements
    """

    DESCRIPTION: str = "Convert usages of `IMetricsService` to the `request.metrics`."

    def __init__(self, context: CodemodContext) -> None:
        super().__init__(context)
        self.has_metrics_import: bool = False
        self.metrics_variable_name: str | None = None
        self.instance_metrics_attribute: str | None = None  # Track self.metrics patterns

    def _is_warehouse_metrics_module(self, module: cst.BaseExpression) -> bool:
        """Check if a module node represents 'warehouse.metrics' or 'warehouse.metrics.interfaces'"""
        # Handle: from warehouse.metrics import IMetricsService
        if (isinstance(module, cst.Attribute)
            and isinstance(module.value, cst.Name)
            and module.value.value == "warehouse"
            and module.attr.value == "metrics"):
            return True

        # Handle: from warehouse.metrics.interfaces import IMetricsService
        if (isinstance(module, cst.Attribute)
            and isinstance(module.value, cst.Attribute)
            and isinstance(module.value.value, cst.Name)
            and module.value.value.value == "warehouse"
            and module.value.attr.value == "metrics"
            and module.attr.value == "interfaces"):
            return True

        return False

    def _is_metrics_service_call(self, call: cst.Call) -> bool:
        """Check if a call is request.find_service(IMetricsService, ...) or self.request.find_service(IMetricsService, ...)"""
        if not (isinstance(call.func, cst.Attribute) and call.func.attr.value == "find_service"):
            return False

        if len(call.args) < 1 or not isinstance(call.args[0].value, cst.Name) or call.args[0].value.value != "IMetricsService":
            return False

        # Handle: request.find_service(IMetricsService, ...)
        if isinstance(call.func.value, cst.Name) and call.func.value.value == "request":
            return True

        # Handle: self.request.find_service(IMetricsService, ...)
        if (isinstance(call.func.value, cst.Attribute) and
            isinstance(call.func.value.value, cst.Name) and
            call.func.value.value.value == "self" and
            call.func.value.attr.value == "request"):
            return True

        return False

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        # Check for import of IMetricsService from warehouse.metrics
        if node.module and self._is_warehouse_metrics_module(node.module):
            if isinstance(node.names, cst.ImportStar):
                return
            for name in node.names:
                if isinstance(name, cst.ImportAlias):
                    if name.name.value == "IMetricsService":
                        self.has_metrics_import = True
                elif isinstance(name, cst.Name):
                    if name.value == "IMetricsService":
                        self.has_metrics_import = True

    def visit_Assign(self, node: cst.Assign) -> None:
        # Look for: metrics = request.find_service(IMetricsService)
        # Also look for: self.metrics = request.find_service(IMetricsService)
        if not self.has_metrics_import:
            return

        if len(node.targets) != 1:
            return

        target = node.targets[0]

        # Check if this is a call to request.find_service(IMetricsService) or self.request.find_service(IMetricsService)
        if isinstance(node.value, cst.Call) and self._is_metrics_service_call(node.value):
            if isinstance(target.target, cst.Name):
                # Handle: metrics = request.find_service(IMetricsService)
                self.metrics_variable_name = target.target.value
            elif isinstance(target.target, cst.Attribute):
                # Handle: self.metrics = request.find_service(IMetricsService) or self.metrics = self.request.find_service(IMetricsService)
                if (isinstance(target.target.value, cst.Name) and
                    target.target.value.value == "self"):
                    self.instance_metrics_attribute = target.target.attr.value

    def leave_Assign(
        self, original_node: cst.Assign, updated_node: cst.Assign
    ) -> Union[cst.Assign, cst.RemovalSentinel]:
        # Remove: metrics = request.find_service(IMetricsService)
        # Remove: self.metrics = request.find_service(IMetricsService)
        if not self.has_metrics_import:
            return updated_node

        if len(updated_node.targets) != 1:
            return updated_node

        target = updated_node.targets[0]

        # Check if this is an assignment we're tracking
        if isinstance(updated_node.value, cst.Call) and self._is_metrics_service_call(updated_node.value):
            if isinstance(target.target, cst.Name) and target.target.value == self.metrics_variable_name:
                return cst.RemovalSentinel.REMOVE
            elif (isinstance(target.target, cst.Attribute) and
                  isinstance(target.target.value, cst.Name) and
                  target.target.value.value == "self" and
                  target.target.attr.value == self.instance_metrics_attribute):
                return cst.RemovalSentinel.REMOVE

        return updated_node

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.Call:
        if not self.has_metrics_import:
            return updated_node

        # Check if this is a call like metrics.increment(), metrics.gauge(), etc.
        if self.metrics_variable_name and isinstance(updated_node.func, cst.Attribute):
            if (isinstance(updated_node.func.value, cst.Name) and
                    updated_node.func.value.value == self.metrics_variable_name):
                # Replace metrics.method() with request.metrics.method()
                new_func = cst.Attribute(
                    value=cst.Attribute(
                        value=cst.Name("request"), attr=cst.Name("metrics")
                    ),
                    attr=updated_node.func.attr,
                )
                return updated_node.with_changes(func=new_func)

        # Check if this is a call like self.metrics.increment(), self.metrics.gauge(), etc.
        if self.instance_metrics_attribute and isinstance(updated_node.func, cst.Attribute):
            if (isinstance(updated_node.func.value, cst.Attribute) and
                isinstance(updated_node.func.value.value, cst.Name) and
                updated_node.func.value.value.value == "self" and
                updated_node.func.value.attr.value == self.instance_metrics_attribute):
                # Replace self.metrics.method() with self.request.metrics.method()
                new_func = cst.Attribute(
                    value=cst.Attribute(
                        value=cst.Attribute(
                            value=cst.Name("self"), attr=cst.Name("request")
                        ),
                        attr=cst.Name("metrics")
                    ),
                    attr=updated_node.func.attr,
                )
                return updated_node.with_changes(func=new_func)

        return updated_node

    def leave_Arg(self, original_node: cst.Arg, updated_node: cst.Arg) -> cst.Arg:
        """Handle inline service calls in function arguments"""
        if not self.has_metrics_import:
            return updated_node

        # Handle metrics=request.find_service(IMetricsService, ...) in function arguments
        if isinstance(updated_node.value, cst.Call) and self._is_metrics_service_call(updated_node.value):
            return updated_node.with_changes(
                value=cst.Attribute(
                    value=cst.Name("request"), attr=cst.Name("metrics")
                )
            )

        return updated_node

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> Union[cst.ImportFrom, cst.RemovalSentinel]:
        if not self.has_metrics_import:
            return updated_node

        # If we have an import from warehouse.metrics, we want to remove it.
        if updated_node.module and self._is_warehouse_metrics_module(updated_node.module):
            return cst.RemovalSentinel.REMOVE
        return updated_node


#### TESTS ####
from libcst.codemod import CodemodTest


class TestConvertMetricsCommand(CodemodTest):

    # The codemod that will be instantiated for us in assertCodemod.
    TRANSFORM = ConvertMetricsCommand

    def test_noop(self) -> None:
        before = """
            request.metrics.increment("foo")
            request.metrics.increment("bar", value=5)
            request.metrics.gauge("baz", 42)
        """
        after = """
            request.metrics.increment("foo")
            request.metrics.increment("bar", value=5)
            request.metrics.gauge("baz", 42)
        """
        self.assertCodemod(before, after)

    def test_substitution(self) -> None:
        before = """
            from warehouse.metrics import IMetricsService
            metrics = request.find_service(IMetricsService)
            metrics.increment("foo")
        """
        after = """
            request.metrics.increment("foo")
        """
        self.assertCodemod(before, after)

    def test_substitution_with_gauge(self) -> None:
        before = """
            from warehouse.metrics import IMetricsService
            fred = request.find_service(IMetricsService)
            fred.gauge("foo", 42)
        """
        after = """
            request.metrics.gauge("foo", 42)
        """
        self.assertCodemod(before, after)

    def test_substitution_with_context(self) -> None:
        before = """
            from warehouse.metrics import IMetricsService
            metrics = request.find_service(IMetricsService, context=None)
            metrics.increment("foo")
        """
        after = """
            request.metrics.increment("foo")
        """
        self.assertCodemod(before, after)

    def test_substitute_in_function(self) -> None:
        before = """
            from warehouse.metrics import IMetricsService

            def on_before_traversal(before_traversal_event):
                request = before_traversal_event.request

                timings = request.timings
                timings["route_match_duration"] = time_ms() - timings["new_request_start"]

                metrics = request.find_service(IMetricsService, context=None)
                metrics.timing(
                    "pyramid.request.duration.route_match", timings["route_match_duration"]
                )
        """
        after = """
            def on_before_traversal(before_traversal_event):
                request = before_traversal_event.request

                timings = request.timings
                timings["route_match_duration"] = time_ms() - timings["new_request_start"]
                request.metrics.timing(
                    "pyramid.request.duration.route_match", timings["route_match_duration"]
                )
        """
        self.assertCodemod(before, after)

    def test_substitute_in_kwargs(self) -> None:
        self.maxDiff = None
        before = """
            from warehouse.metrics import IMetricsService

            def database_login_factory(context, request):
                return DatabaseUserService(
                    request.db,
                    metrics=request.find_service(IMetricsService, context=None),
                    remote_addr=request.remote_addr,
                    ratelimiters={
                        "ip.login": request.find_service(
                            IRateLimiter, name="ip.login", context=None
                        ),
                        "global.login": request.find_service(
                            IRateLimiter, name="global.login", context=None
                        ),
                        "user.login": request.find_service(
                            IRateLimiter, name="user.login", context=None
                        ),
                        "email.add": request.find_service(
                            IRateLimiter, name="email.add", context=None
                        ),
                        "password.reset": request.find_service(
                            IRateLimiter, name="password.reset", context=None
                        ),
                    },
                )
        """
        after = """
            def database_login_factory(context, request):
                return DatabaseUserService(
                    request.db,
                    metrics=request.metrics,
                    remote_addr=request.remote_addr,
                    ratelimiters={
                        "ip.login": request.find_service(
                            IRateLimiter, name="ip.login", context=None
                        ),
                        "global.login": request.find_service(
                            IRateLimiter, name="global.login", context=None
                        ),
                        "user.login": request.find_service(
                            IRateLimiter, name="user.login", context=None
                        ),
                        "email.add": request.find_service(
                            IRateLimiter, name="email.add", context=None
                        ),
                        "password.reset": request.find_service(
                            IRateLimiter, name="password.reset", context=None
                        ),
                    },
                )
        """
        self.assertCodemod(before, after)

    def test_substitute_in_class_method(self) -> None:
        before = """
            from warehouse.metrics import IMetricsService

            class MyView:
                def __init__(self, request):
                    self.request = request
                    self.metrics = request.find_service(IMetricsService, context=None)

                def my_method(self):
                    self.metrics.increment("my_metric")
        """
        after = """
            class MyView:
                def __init__(self, request):
                    self.request = request

                def my_method(self):
                    self.request.metrics.increment("my_metric")
        """
        self.assertCodemod(before, after)

    def test_substitute_in_class_method_with_self_request(self) -> None:
        before = """
            from warehouse.metrics import IMetricsService

            class MyView:
                def __init__(self, request):
                    self.request = request
                    self.metrics = self.request.find_service(IMetricsService, context=None)

                def my_method(self):
                    self.metrics.increment("my_metric")
        """
        after = """
            class MyView:
                def __init__(self, request):
                    self.request = request

                def my_method(self):
                    self.request.metrics.increment("my_metric")
        """
        self.assertCodemod(before, after)

    def test_substitute_with_interfaces_import(self) -> None:
        before = """
            from warehouse.metrics.interfaces import IMetricsService
            metrics = request.find_service(IMetricsService)
            metrics.increment("foo")
        """
        after = """
            request.metrics.increment("foo")
        """
        self.assertCodemod(before, after)


if __name__ == "__main__":
    import unittest

    unittest.main()
