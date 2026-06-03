import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


@router.get("/compare")
def compare_stops_api():
    """对比技术面 vs 技术面+基本面止盈止损。"""
    from update_stops import compare_stops
    return compare_stops()


@router.get("/compare/page")
def compare_page():
    return FileResponse(os.path.join(STATIC_DIR, "stops_compare.html"))
