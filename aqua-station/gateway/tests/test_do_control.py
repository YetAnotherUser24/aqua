"""
Tests for manual DO closed-loop control API endpoints (/api/control/do).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from aqua_gateway.api.app import DOControlRequest, create_app
from aqua_gateway.main import Gateway
from aqua_gateway.schema.registry import load_registry


def test_do_control_request_validation() -> None:
    req = DOControlRequest(setpoint_mg_l=6.5, kp=221.65, ki=1.915, beta=0.35, tt=100.0, tau_lag=30.0)
    assert req.setpoint_mg_l == 6.5
    assert req.kp == 221.65
    assert req.beta == 0.35
    assert req.mode == "pid"

    # Negative setpoint rejected
    with pytest.raises(ValidationError):
        DOControlRequest(setpoint_mg_l=-1.0)

    # Beta > 1.0 rejected
    with pytest.raises(ValidationError):
        DOControlRequest(setpoint_mg_l=6.0, beta=1.5)


@pytest.mark.asyncio
async def test_do_control_endpoints() -> None:
    gateway = object.__new__(Gateway)
    gateway.settings = type("S", (), {"timezone": "America/Lima"})()
    gateway.commands = MagicMock()
    cmd1 = MagicMock(id="cmd-1")
    cmd2 = MagicMock(id="cmd-2")
    gateway.commands.issue = AsyncMock(side_effect=[cmd1, cmd2, cmd1, cmd2])
    gateway.events = MagicMock()
    gateway.events.emit = AsyncMock()
    gateway.live = MagicMock()
    gateway.live.snapshot = MagicMock(return_value={
        "nodes": {
            "aerator": {"metrics": {"drv_rpm": 250.0, "ctl_mode": "pid", "drv_running": True}},
            "od_sensor": {"metrics": {"od_do_mg_l": 5.82}},
        }
    })

    reg = load_registry()
    aerator_schema = reg.for_role("aerator")
    gateway.registry = MagicMock()
    gateway.registry.roles = {"aerator": aerator_schema}
    gateway.registry.for_role = MagicMock(return_value=aerator_schema)

    app = create_app(gateway)

    # 1. GET /api/control/do
    for route in app.routes:
        if getattr(route, "path", None) == "/api/control/do" and "GET" in route.methods:
            get_handler = route.endpoint
            res = await get_handler()
            assert res["defaults"]["kp"] == 221.65
            assert res["current"]["do_mg_l"] == 5.82
            assert res["current"]["ctl_mode"] == "pid"
            assert res["current"]["running"] is True
            break
    else:
        pytest.fail("GET /api/control/do not found in routes")

    # 2. POST /api/control/do
    for route in app.routes:
        if getattr(route, "path", None) == "/api/control/do" and "POST" in route.methods:
            post_handler = route.endpoint
            body = DOControlRequest(setpoint_mg_l=7.0, kp=200.0, ki=1.5, beta=0.4, tt=90.0, tau_lag=25.0)
            res = await post_handler(body)
            assert res["status"] == "running"
            assert res["setpoint_mg_l"] == 7.0
            assert res["params"]["kp"] == 200.0
            assert res["commands"]["set_config"] == "cmd-1"
            assert res["commands"]["start"] == "cmd-2"
            break
    else:
        pytest.fail("POST /api/control/do not found in routes")

    # 3. POST /api/control/do/stop
    for route in app.routes:
        if getattr(route, "path", None) == "/api/control/do/stop" and "POST" in route.methods:
            stop_handler = route.endpoint
            res = await stop_handler()
            assert res["status"] == "stopped"
            assert res["mode"] == "manual"
            break
    else:
        pytest.fail("POST /api/control/do/stop not found in routes")
