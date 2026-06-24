"""Shared edge behaviour key → Mongolian label map.

Lives in the edge layer (NOT gui) so the live-view overlay can label behaviours
WITHOUT importing from gui (which would be a circular dependency: gui imports
edge, never the reverse). The gui «Зан үйл» registry uses the same labels.
"""

from __future__ import annotations

BEHAVIOR_LABELS: dict[str, str] = {
    "item_pickup": "Эд зүйл барих",
    "wrist_to_torso": "Гар бие рүү",
    "conceal": "Эд зүйл нуух",
    "repeated_shelf_visit": "Тавиур давтан зочлох",
    "exit_after_concealment": "Нуусны дараа гарц руу",
}
