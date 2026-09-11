"""Catalog endpoints: materials, dies, punches, press brakes."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..models import Die, Material, PressBrake, Punch
from ..runtime import get_store

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/materials")
def materials():
    return get_store().list_catalog("material")


@router.get("/dies")
def dies():
    return get_store().list_catalog("die")


@router.get("/punches")
def punches():
    return get_store().list_catalog("punch")


@router.get("/presses")
def presses():
    return get_store().list_catalog("press")
