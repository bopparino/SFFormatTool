"""Amber Alabaster palette constants for explicit widget overrides.

Mirrors the design tokens used by the Insight and cut-sheet apps so all
three share a visual identity. customtkinter's theme.json handles defaults;
this module supplies values for the specific widgets that need explicit
fg_color / text_color / border_color overrides.
"""

# Foundation
BODY_BG     = "#F9F9F8"
CARD_BG     = "#FFFFFF"
INPUT_BG    = "#FAFAFA"
SIDEBAR_BG  = "#1A1A1A"

# Neutral scale (warm)
GRAY_50  = "#FAFAF9"
GRAY_100 = "#F5F5F4"
GRAY_200 = "#E7E5E4"
GRAY_300 = "#D6D3D1"
GRAY_400 = "#A8A29E"
GRAY_600 = "#57534E"
GRAY_800 = "#1A1A1A"
GRAY_900 = "#0C0C0C"

# Safety Orange accent
ACCENT      = "#FF8C00"
ACCENT_MID  = "#E07B00"
ACCENT_SOFT = "#FFB347"

# Semantic
SUCCESS = "#10B981"
DANGER  = "#EF4444"

# Drop zone state colors
DROP_IDLE_BG     = CARD_BG
DROP_IDLE_BORDER = GRAY_300
DROP_LOADED_BG   = "#FFF7ED"  # warm amber tint
DROP_LOADED_BORDER = ACCENT

# Status console — kept as a dark "data terminal" inset to echo Insight's
# sidebar contrast. Light app, dark console — same family ethos.
CONSOLE_BG   = SIDEBAR_BG
CONSOLE_TEXT = GRAY_50
CONSOLE_DIM  = GRAY_400
