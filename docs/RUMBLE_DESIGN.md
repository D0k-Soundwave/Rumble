# RUMBLE — Design & UI Specification
## Visual Design System

> **Status (2026-06-30): historical design brief.** This captures the original
> visual-design intent (colour system, component styling, layout mockups) and
> remains a useful reference for the token/QSS architecture. The app is now fully
> built and several specifics have since evolved:
> - The visual language pivoted to a **"rounded glass HUD"** look (glassmorphism
>   × sci-fi HUD): translucent glass surfaces, a backdrop gradient, per-widget
>   glow/elevation (`ui/theme/effects.py`), and a painted accent **corner-tick
>   frame** (`ui/hud_frame.py`) around the EQ graph — superseding the original
>   "no gradients or drop shadows" rule below.
> - The theme set is now **9 themes + System** (RUMBLE, Nocturne, Crimson,
>   Ember, Verdant, Aurora, Titanium, Daylight, Parchment), **not** the four
>   dark themes sketched below. Themes carry optional structural levers
>   (`radius_scale`/`sheen`/`bevel`/`glow`/`glass_alpha`).
> - The window is a **fixed 1000×580 compact** layout with an optional **Double
>   (2×)** size (not freely resizable), and the left panel is a **searchable tag
>   tree** (User / Built-in branches), not a flat list with a filter dropdown.
>
> For the authoritative current behaviour and feature set see
> **`RUMBLE_SPECIFICATION.md` §8.6**.

---

## Design Philosophy

RUMBLE should look and feel like professional audio software — the category that includes FabFilter, iZotope, and Waves. The design language is:

- **Precision over decoration** — every visual element earns its place
- **Dark-first** — audio engineers work in dark environments; dark is the canonical theme
- **Depth through subtlety** — use layering and tone, not gradients or drop shadows
- **Typographic clarity** — numbers and labels must be instantly readable at a glance
- **Responsive to touch** — controls should feel responsive; hover/active states must be immediate

The application should feel expensive. Someone opening it for the first time should think it cost money.

---

## Theming Architecture

### Implementation Strategy

Themes are implemented as Python dictionaries of color tokens. A `ThemeEngine` class generates a complete QSS stylesheet string from the active token dict, then applies it to the `QApplication` instance. Switching themes at runtime requires only regenerating and reapplying the stylesheet.

**Do NOT use qt-material or any third-party theme library.** Build a custom QSS system from scratch for full control over every pixel.

**Base style:** Set `QApplication.setStyle("Fusion")` before applying any stylesheet. Fusion is the cleanest Qt base style and takes QSS overrides reliably across all Windows versions.

### Theme Files Structure

```
rumble/
└── ui/
    └── theme/
        ├── theme_engine.py     ← generates QSS from token dict
        ├── tokens.py           ← all theme token dicts
        └── themes/
            ├── void.py         ← Dark / Blue-Purple (DEFAULT)
            ├── obsidian.py     ← Dark / Amber
            ├── carbon.py       ← Dark / Cyan
            └── daylight.py     ← Light / Blue
```

### Token Dict Structure

```python
THEME = {
    # Backgrounds
    "bg_deep":        "#0B0B0F",   # Deepest background, titlebar area
    "bg_base":        "#12121A",   # Main window background
    "bg_surface":     "#1A1A26",   # Panel/card backgrounds
    "bg_raised":      "#222234",   # Elevated elements (dropdowns, tooltips)
    "bg_sunken":      "#09090E",   # Inset elements (input fields, graph)

    # Borders
    "border_subtle":  "#1E1E30",   # Barely-there dividers
    "border_default": "#2E2E46",   # Standard borders
    "border_strong":  "#44446A",   # Focused/active borders

    # Accent (primary interactive color)
    "accent":         "#5E81F4",   # Primary accent
    "accent_hover":   "#7B97FF",   # Accent hovered
    "accent_active":  "#4A68E8",   # Accent pressed
    "accent_muted":   "#5E81F428", # Accent at low opacity (selection bg)
    "accent_glow":    "#5E81F440", # For graph curve glow

    # Text
    "text_primary":   "#E8E8F8",   # Headings, primary labels
    "text_secondary": "#8888AA",   # Secondary labels, hints
    "text_muted":     "#555570",   # Disabled text, placeholders
    "text_on_accent": "#FFFFFF",   # Text on accent-coloured backgrounds

    # Semantic
    "success":        "#4ECDC4",   # Active profile indicator, positive values
    "warning":        "#FFB347",   # Warnings, clip risk indicator
    "error":          "#FF6B6B",   # Errors, invalid inputs
    "info":           "#5E81F4",   # Same as accent

    # Graph-specific
    "graph_bg":       "#08080E",   # Graph canvas background
    "graph_grid":     "#1A1A2A",   # Grid lines
    "graph_zero":     "#2E2E4E",   # 0 dB reference line
    "graph_curve":    "#5E81F4",   # Combined EQ curve
    "graph_band":     "#5E81F480", # Individual band curves (translucent)
    "graph_band_sel": "#FFB347",   # Selected/highlighted band curve
    "graph_handle":   "#5E81F4",   # Draggable band handle dots
    "graph_text":     "#44446A",   # Axis labels

    # Scrollbars
    "scrollbar_track": "#12121A",
    "scrollbar_thumb": "#2E2E46",
    "scrollbar_thumb_hover": "#44446A",

    # Accent name for display
    "name":           "Void",
    "accent_name":    "Blue-Purple",
}
```

---

## The Four Built-In Themes

### 1. Void (Default Dark — Blue-Purple)

```python
VOID = {
    "bg_deep":         "#0B0B0F",
    "bg_base":         "#12121A",
    "bg_surface":      "#1A1A26",
    "bg_raised":       "#222234",
    "bg_sunken":       "#09090E",
    "border_subtle":   "#1E1E30",
    "border_default":  "#2E2E46",
    "border_strong":   "#5E81F4",
    "accent":          "#5E81F4",
    "accent_hover":    "#7B97FF",
    "accent_active":   "#4A68E8",
    "accent_muted":    "#5E81F428",
    "accent_glow":     "#5E81F440",
    "text_primary":    "#E8E8F8",
    "text_secondary":  "#8888AA",
    "text_muted":      "#555570",
    "text_on_accent":  "#FFFFFF",
    "success":         "#4ECDC4",
    "warning":         "#FFB347",
    "error":           "#FF6B6B",
    "graph_bg":        "#08080E",
    "graph_grid":      "#1A1A2A",
    "graph_zero":      "#2E2E4E",
    "graph_curve":     "#5E81F4",
    "graph_band":      "#5E81F460",
    "graph_band_sel":  "#FFB347",
    "graph_handle":    "#5E81F4",
    "graph_text":      "#44446A",
    "scrollbar_track": "#12121A",
    "scrollbar_thumb": "#2E2E46",
    "scrollbar_thumb_hover": "#44446A",
    "name": "Void",
}
```

### 2. Obsidian (Dark — Amber)

Same structure, accent changed to:
```python
"accent":         "#FF8C42",
"accent_hover":   "#FFA664",
"accent_active":  "#E07330",
"accent_muted":   "#FF8C4228",
"accent_glow":    "#FF8C4240",
"graph_curve":    "#FF8C42",
"graph_band":     "#FF8C4260",
"graph_handle":   "#FF8C42",
"border_strong":  "#FF8C42",
"name": "Obsidian",
```

### 3. Carbon (Dark — Cyan)

```python
"accent":         "#00D4FF",
"accent_hover":   "#33DDFF",
"accent_active":  "#00AADD",
"accent_muted":   "#00D4FF28",
"accent_glow":    "#00D4FF40",
"graph_curve":    "#00D4FF",
"graph_band":     "#00D4FF60",
"graph_handle":   "#00D4FF",
"border_strong":  "#00D4FF",
"name": "Carbon",
```

### 4. Daylight (Light — Blue)

```python
"bg_deep":         "#E8E8F0",
"bg_base":         "#F0F0F8",
"bg_surface":      "#FFFFFF",
"bg_raised":       "#FFFFFF",
"bg_sunken":       "#E0E0EC",
"border_subtle":   "#D0D0E0",
"border_default":  "#B8B8D0",
"border_strong":   "#4A68E8",
"accent":          "#4A68E8",
"accent_hover":    "#6680F0",
"accent_active":   "#3A58D8",
"accent_muted":    "#4A68E820",
"text_primary":    "#12121A",
"text_secondary":  "#555570",
"text_muted":      "#9090A8",
"text_on_accent":  "#FFFFFF",
"success":         "#2AB5AC",
"warning":         "#E08020",
"error":           "#D94040",
"graph_bg":        "#E8E8F4",
"graph_grid":      "#D0D0E4",
"graph_zero":      "#B0B0CC",
"graph_curve":     "#4A68E8",
"graph_band":      "#4A68E860",
"graph_band_sel":  "#E08020",
"graph_handle":    "#4A68E8",
"graph_text":      "#9090A8",
"scrollbar_track": "#E0E0EC",
"scrollbar_thumb": "#B8B8D0",
"scrollbar_thumb_hover": "#9898B8",
"name": "Daylight",
```

---

## Typography

### Font Stack

**Primary font:** `"Inter"` → fall back to `"Segoe UI"` → `"Arial"` → `sans-serif`

Inter is a clean, purpose-built UI font with excellent number rendering (critical for displaying dB and Hz values). Bundle Inter as a resource file inside the application. Load it via `QFontDatabase.addApplicationFont()` on startup.

Download Inter from: https://rsms.me/inter/ (SIL Open Font License — free to bundle)

### Type Scale

| Role | Font | Size | Weight | Usage |
|------|------|------|--------|-------|
| `display` | Inter | 18px | 600 | Window title, profile name in header |
| `heading` | Inter | 13px | 600 | Panel headings, section labels |
| `body` | Inter | 12px | 400 | Default UI text, labels |
| `body_medium` | Inter | 12px | 500 | Filter type labels, active states |
| `small` | Inter | 11px | 400 | Secondary labels, hints, tooltips |
| `mono` | `"Cascadia Code"` → `"Consolas"` | 11px | 400 | Raw config view, frequency values |
| `numeric` | Inter | 12px | 500 (tabular nums) | All dB, Hz, Q values |

Enable tabular number rendering for all numeric fields to prevent layout shift as values change:
```python
font.setFeature(QFont.Feature.TabularFigures, 1)
# Or in QSS: font-variant-numeric: tabular-nums;
```

---

## Spacing System

Use an 8px base grid. All padding, margin, and gap values should be multiples of 4px.

| Token | Value | Usage |
|-------|-------|-------|
| `space_1` | 4px | Tight — icon padding, small gaps |
| `space_2` | 8px | Default component padding |
| `space_3` | 12px | Section padding |
| `space_4` | 16px | Panel padding, between groups |
| `space_5` | 24px | Major section gaps |
| `space_6` | 32px | Large section separation |

### Border Radius

| Token | Value | Usage |
|-------|-------|-------|
| `radius_sm` | 3px | Input fields, small buttons |
| `radius_md` | 6px | Panels, cards, dropdowns |
| `radius_lg` | 10px | Main panels, large surfaces |

---

## Component Specifications

### Window

```
Minimum size:  1100 × 700 px
Default size:  1280 × 800 px
Title bar:     Native Windows title bar + custom menu bar
Window icon:   rumble.ico (32×32 and 16×16)
```

Main window background: `bg_base`. No gradients on the base.

### Menubar (top)

Height: 32px. Background: `bg_deep`. Text: `text_secondary`. Hover: `bg_surface` background + `text_primary`. Selected: `accent_muted` background.

### Status Bar (bottom)

Height: 24px. Background: `bg_deep`. Font: `small`. Left: active profile name + status. Right: APO path. Separator: `border_subtle`.

### Profile Panel (left sidebar)

Width: 240px (fixed, not resizable in v1). Background: `bg_surface`. Right border: `border_subtle` 1px.

**Header:**
- Height: 48px. Text: "PROFILES" in `heading` style, `text_muted` colour. Padding: 16px.
- `[+ New]` button right-aligned.

**Profile list item:**
- Height: 52px. Padding: 12px 16px.
- Name: `body_medium`, `text_primary`.
- Subtitle: tag list or modified date, `small`, `text_muted`.
- Left border: 3px transparent. On active profile: 3px `accent` solid.
- Hover: `bg_raised` background.
- Selected: `accent_muted` background.
- Active indicator dot: 8px circle, `success` colour, right side of row.

**Tag filter row:**
- Below list. Horizontal scrollable pill buttons.
- Pill: `bg_raised` bg, `border_default` border, `text_secondary` text, `radius_lg`.
- Active pill: `accent` bg, `text_on_accent` text.

**Bottom buttons:**
- `[Import]` `[Export]` `[Backup]` — icon + text, `small`, spaced 4px apart.
- Background: `bg_surface`. Border-top: `border_subtle`.

### Editor Panel (main area)

Background: `bg_base`. Left border: none (sidebar provides separation).

**Profile header bar:**
- Height: 56px. Background: `bg_surface`. Bottom border: `border_subtle`.
- Profile name: editable `QLineEdit`, `display` font, `text_primary`. No border until focused; on focus: bottom border `accent` 2px.
- Description: smaller `QLineEdit` below name, `small`, `text_secondary`.
- `[Save]` button (accent filled), `[Activate]` button (success filled), `[Bypass]` button (warning outlined) — right-aligned.
- Active status: `● ACTIVE` badge in `success` colour when active.

**Controls row:**
- Height: 40px. Background: `bg_base`. Padding: 0 16px.
- `Device:` label + dropdown. `Channel:` label + dropdown. `Preamp:` label + spinbox + `[Auto]` button.
- All controls same height (28px). Font: `body`.

### Frequency Response Graph

**Container:** Background `graph_bg`. Border: `border_subtle` 1px. Radius: `radius_md`. Margin: 16px. Height: 220px minimum.

**pyqtgraph configuration:**
```python
graph.setBackground(theme["graph_bg"])
graph.showGrid(x=True, y=True)
graph.getAxis("bottom").setPen(theme["graph_text"])
graph.getAxis("left").setPen(theme["graph_text"])
graph.getAxis("bottom").setTextPen(theme["graph_text"])
graph.getAxis("left").setTextPen(theme["graph_text"])
```

**Grid lines:** colour `graph_grid`, width 1px.

**0 dB reference line:** colour `graph_zero`, width 1px, style dashed.

**Individual band curves:**
- Colour: `graph_band` (translucent accent).
- Width: 1px.
- Shown only when mouse hovers corresponding band row.

**Combined curve:**
- Colour: `graph_curve`.
- Width: 2.5px.
- Glow effect: render same curve twice — once at width 8px with colour `accent_glow`, once at 2.5px with `graph_curve` on top.

**Band handles (draggable):**
- Circle, 10px diameter.
- Fill: `graph_handle`. Border: `bg_sunken` 2px.
- Hover: scale to 14px diameter, add glow.
- Selected: fill `graph_band_sel`.

**Axis labels:**
- X: 20, 50, 100, 200, 500, 1k, 2k, 5k, 10k, 20k Hz.
- Y: -24, -18, -12, -6, 0, +6, +12, +18, +24 dB.
- Font: `mono` 10px, colour `graph_text`.

**Clip warning zone:** When combined curve exceeds 0 dB without adequate preamp, shade the positive region with `error` at 10% opacity.

### Filter Band Table

Container: background `bg_surface`. Border: `border_subtle`. Radius: `radius_md`. Margin: 0 16px 16px. Padding: 0.

**Table header row:**
- Height: 32px. Background: `bg_deep`. Font: `small`, `text_muted`, uppercase.
- Columns: `#` (32px) | `On` (36px) | `Type` (120px) | `Frequency` (110px) | `Gain` (90px) | `Q / BW` (90px) | `del` (36px).

**Filter row:**
- Height: 44px. Background: `bg_surface`. Hover: `bg_raised`.
- Selected (cursor focus): left border 2px `accent`.
- Alternating rows: even rows `bg_sunken` at very low opacity (5%).
- Divider between rows: `border_subtle` 1px.

**Band number `#`:** `small`, `text_muted`, centered.

**Enable toggle:**
- Custom `QCheckBox` styled as a pill toggle switch.
- On: fill `accent`. Off: fill `border_default`.
- Transition: instant (no animation — audio tools should be snappy).
- Size: 28×16px.

**Filter type dropdown:**
- `QComboBox`. Background `bg_sunken`. Border `border_default` 1px. Radius `radius_sm`.
- Height: 28px. Font: `body_medium`.
- Arrow: SVG chevron in `text_muted`.
- Dropdown list: background `bg_raised`. Item hover: `accent_muted`.
- Group separators between filter categories in the dropdown:
  - Peaking: PK, PEQ
  - Pass filters: LP, LPQ, HP, HPQ, BP
  - Shelf filters: LS, LSC, LS 6dB, LS 12dB, HS, HSC, HS 6dB, HS 12dB
  - Special: NO, AP
  - Advanced: IIR

**Frequency / Gain / Q spinboxes:**
- `QDoubleSpinBox`. Background `bg_sunken`. Border `border_default` 1px. Radius `radius_sm`.
- Height: 28px. Font: `numeric`.
- Focus: border changes to `accent` 1px.
- Disabled (field not applicable to selected type): background `bg_deep`, text `text_muted`, border `border_subtle`.
- Suffix text (Hz / dB): `text_muted`.
- Up/down arrows: hidden (use scroll wheel and direct typing only — cleaner look).

**Delete button:**
- Icon only (`×`). Size: 20×20px. Background: transparent.
- Hover: background `error` at 20%, icon colour `error`.

**`[+ Add Band]` button:**
- Full-width, below the table. Height: 36px. Background: `bg_sunken`. Border: `border_subtle` 1px dashed. Radius `radius_sm`.
- Hover: background `accent_muted`, border `accent`.
- Icon: `+` in `text_muted`. Text: "Add Filter Band" in `small`, `text_muted`.

### Buttons

**Primary (Filled Accent):**
```
Background: accent
Text: text_on_accent
Height: 32px
Padding: 0 16px
Radius: radius_sm
Hover: accent_hover background
Pressed: accent_active background
Font: body_medium
```

**Secondary (Outlined):**
```
Background: transparent
Border: border_default 1px
Text: text_primary
Height: 32px
Padding: 0 16px
Radius: radius_sm
Hover: bg_raised background, border_strong border
Pressed: bg_sunken background
```

**Danger (Outlined Red):**
```
Background: transparent
Border: error 1px
Text: error
Hover: error at 15% background
```

**Ghost (no border):**
```
Background: transparent
Text: text_secondary
Hover: bg_raised background, text_primary text
```

**Icon Button (square):**
```
Size: 28×28px
Background: transparent
Icon: text_muted
Hover: bg_raised background, text_primary icon
Radius: radius_sm
```

### Input Fields (QLineEdit / QDoubleSpinBox)

```
Background: bg_sunken
Border: border_default 1px
Border-radius: radius_sm
Height: 32px
Padding: 0 8px
Font: body / numeric
Color: text_primary
Placeholder: text_muted

Focus:
  Border: accent 1px
  No outline glow (clean look)

Error state:
  Border: error 1px
  Background: error at 8% opacity
```

### Dropdowns (QComboBox)

```
Background: bg_sunken
Border: border_default 1px
Radius: radius_sm
Height: 32px
Padding: 0 8px
Font: body

Popup:
  Background: bg_raised
  Border: border_strong 1px
  Radius: radius_md
  Max height: 300px (scrollable)
  Item height: 32px
  Item hover: accent_muted background
  Item selected: accent background, text_on_accent
  Separator: border_subtle 1px
```

### Scrollbars

```
Width/Height: 6px (thin, modern)
Track: scrollbar_track
Thumb: scrollbar_thumb
Thumb hover: scrollbar_thumb_hover
Radius: 3px
No arrows (remove QScrollBar::add-line, sub-line)
```

### Tooltips

```
Background: bg_raised
Border: border_default 1px
Radius: radius_sm
Padding: 6px 10px
Font: small
Color: text_primary
Max width: 280px
```

### Panels and Collapsible Sections

**Section header:**
- Height: 40px. Background: `bg_surface`. Bottom border: `border_subtle`.
- `▶` / `▼` chevron: `text_muted`, 10px. Rotates on expand.
- Title: `heading`, `text_secondary`.
- Hover: `bg_raised`.

**Section body:**
- Background: `bg_base`. Padding: `space_4`.

### Tabs (for Advanced section sub-panels)

```
Tab bar background: bg_deep
Tab: height 36px, padding 0 16px
Tab text: small, text_muted
Tab hover: text_secondary
Tab selected:
  background: bg_surface
  text: text_primary
  top border: accent 2px
  bottom border: none
```

### Dialogs and Modal Windows

```
Background: bg_surface
Border: border_default 1px
Radius: radius_lg
Shadow: none (flat design)
Padding: space_5
Minimum width: 400px
Title: heading, text_primary
Button row: right-aligned, gap space_2
```

### System Tray Menu

Closing the main window always minimises to tray — this is mandatory behaviour. RUMBLE must always remain in the tray. The only full exit path is the Quit item in the tray menu.

**Tray menu item order:**
1. RUMBLE (app name, disabled — acts as header)
2. Separator
3. Profile list (one item per profile; active profile shown with accent colour and bullet)
4. Separator
5. Bypass EQ (toggle)
6. Separator
7. Open RUMBLE
8. Settings
9. Separator
10. Quit RUMBLE

```
Background: bg_raised
Border: border_default 1px
Radius: radius_md
Padding: space_1

Item: height 32px, padding 0 12px
Item font: body
Item hover: accent_muted background
Separator: border_subtle 1px
Active profile item: body_medium, accent colour, accent_muted background
App name header: small, text_muted, non-interactive
Quit item: body, error colour on hover
```

### Progress Bar (for backup/restore operations)

```
Height: 4px
Background: bg_sunken
Fill: accent
Radius: 2px
No text inside bar
Show percentage as label above/below
```

### Health Check / Warning Banner

```
Background: warning at 15%
Border-left: warning 4px solid
Padding: 12px 16px
Radius: radius_sm
Icon: warning.svg in warning colour
Text: body, text_primary
Close button: ghost icon button, right side
```

---

## QSS Implementation

### ThemeEngine class

```python
class ThemeEngine:
    def __init__(self, token_dict: dict):
        self.t = token_dict

    def apply(self, app):
        app.setStyle("Fusion")
        app.setStyleSheet(self._generate_qss())

    def switch(self, app, new_token_dict: dict):
        self.t = new_token_dict
        self.apply(app)

    def _generate_qss(self) -> str:
        t = self.t
        return f"""
/* ─── Base ─── */
QWidget {{
    background-color: {t['bg_base']};
    color: {t['text_primary']};
    font-family: "Inter", "Segoe UI", Arial, sans-serif;
    font-size: 12px;
    border: none;
}}

/* ─── Main Window ─── */
QMainWindow {{
    background-color: {t['bg_base']};
}}

/* ─── Panels / Frames ─── */
QFrame[class="panel"] {{
    background-color: {t['bg_surface']};
    border: 1px solid {t['border_subtle']};
    border-radius: 6px;
}}

/* ─── Buttons ─── */
QPushButton {{
    background-color: transparent;
    color: {t['text_primary']};
    border: 1px solid {t['border_default']};
    border-radius: 3px;
    padding: 0 16px;
    height: 32px;
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: {t['bg_raised']};
    border-color: {t['border_strong']};
}}
QPushButton:pressed {{
    background-color: {t['bg_sunken']};
}}
QPushButton[class="primary"] {{
    background-color: {t['accent']};
    color: {t['text_on_accent']};
    border: none;
}}
QPushButton[class="primary"]:hover {{
    background-color: {t['accent_hover']};
}}
QPushButton[class="primary"]:pressed {{
    background-color: {t['accent_active']};
}}
QPushButton[class="success"] {{
    background-color: {t['success']};
    color: {t['bg_deep']};
    border: none;
}}
QPushButton[class="danger"] {{
    background-color: transparent;
    color: {t['error']};
    border: 1px solid {t['error']};
}}
QPushButton[class="danger"]:hover {{
    background-color: {t['error']}26;
}}
QPushButton[class="ghost"] {{
    background-color: transparent;
    border: none;
    color: {t['text_secondary']};
}}
QPushButton[class="ghost"]:hover {{
    background-color: {t['bg_raised']};
    color: {t['text_primary']};
}}
QPushButton:disabled {{
    color: {t['text_muted']};
    border-color: {t['border_subtle']};
}}

/* ─── Input Fields ─── */
QLineEdit, QDoubleSpinBox, QSpinBox {{
    background-color: {t['bg_sunken']};
    color: {t['text_primary']};
    border: 1px solid {t['border_default']};
    border-radius: 3px;
    padding: 0 8px;
    height: 32px;
    font-size: 12px;
}}
QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus {{
    border-color: {t['accent']};
}}
QLineEdit:disabled, QDoubleSpinBox:disabled {{
    background-color: {t['bg_deep']};
    color: {t['text_muted']};
    border-color: {t['border_subtle']};
}}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,
QSpinBox::up-button, QSpinBox::down-button {{
    width: 0px;
    border: none;
}}

/* ─── ComboBox ─── */
QComboBox {{
    background-color: {t['bg_sunken']};
    color: {t['text_primary']};
    border: 1px solid {t['border_default']};
    border-radius: 3px;
    padding: 0 8px;
    height: 32px;
}}
QComboBox:focus {{
    border-color: {t['accent']};
}}
QComboBox:disabled {{
    color: {t['text_muted']};
    background-color: {t['bg_deep']};
}}
QComboBox::drop-down {{
    width: 24px;
    border: none;
}}
QComboBox QAbstractItemView {{
    background-color: {t['bg_raised']};
    color: {t['text_primary']};
    border: 1px solid {t['border_strong']};
    border-radius: 6px;
    selection-background-color: {t['accent_muted']};
    selection-color: {t['text_primary']};
    outline: none;
}}

/* ─── Labels ─── */
QLabel[class="heading"] {{
    color: {t['text_secondary']};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1px;
}}
QLabel[class="display"] {{
    color: {t['text_primary']};
    font-size: 18px;
    font-weight: 600;
}}
QLabel[class="muted"] {{
    color: {t['text_muted']};
    font-size: 11px;
}}
QLabel[class="badge-live"] {{
    color: {t['success']};
    font-size: 11px;
    font-weight: 600;
}}

/* ─── Scrollbars ─── */
QScrollBar:vertical {{
    background: {t['scrollbar_track']};
    width: 6px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {t['scrollbar_thumb']};
    border-radius: 3px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {t['scrollbar_thumb_hover']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar:horizontal {{
    background: {t['scrollbar_track']};
    height: 6px;
}}
QScrollBar::handle:horizontal {{
    background: {t['scrollbar_thumb']};
    border-radius: 3px;
    min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {t['scrollbar_thumb_hover']};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

/* ─── Tooltips ─── */
QToolTip {{
    background-color: {t['bg_raised']};
    color: {t['text_primary']};
    border: 1px solid {t['border_default']};
    border-radius: 3px;
    padding: 6px 10px;
    font-size: 11px;
}}

/* ─── Splitter ─── */
QSplitter::handle {{
    background-color: {t['border_subtle']};
    width: 1px;
}}

/* ─── Tab Widget ─── */
QTabWidget::pane {{
    background-color: {t['bg_surface']};
    border: 1px solid {t['border_subtle']};
    border-radius: 6px;
}}
QTabBar::tab {{
    background-color: {t['bg_deep']};
    color: {t['text_muted']};
    padding: 0 16px;
    height: 36px;
    font-size: 11px;
}}
QTabBar::tab:selected {{
    background-color: {t['bg_surface']};
    color: {t['text_primary']};
    border-top: 2px solid {t['accent']};
}}
QTabBar::tab:hover {{
    color: {t['text_secondary']};
}}

/* ─── Table / List ─── */
QTableWidget, QListWidget, QTreeWidget {{
    background-color: {t['bg_surface']};
    alternate-background-color: {t['bg_sunken']};
    border: 1px solid {t['border_subtle']};
    gridline-color: {t['border_subtle']};
    outline: none;
}}
QHeaderView::section {{
    background-color: {t['bg_deep']};
    color: {t['text_muted']};
    border: none;
    border-bottom: 1px solid {t['border_default']};
    padding: 0 8px;
    height: 32px;
    font-size: 11px;
    font-weight: 600;
}}

/* ─── Menu ─── */
QMenu {{
    background-color: {t['bg_raised']};
    border: 1px solid {t['border_default']};
    border-radius: 6px;
    padding: 4px 0;
}}
QMenu::item {{
    height: 32px;
    padding: 0 12px;
    color: {t['text_primary']};
    font-size: 12px;
}}
QMenu::item:selected {{
    background-color: {t['accent_muted']};
}}
QMenu::separator {{
    height: 1px;
    background: {t['border_subtle']};
    margin: 4px 0;
}}

/* ─── Status Bar ─── */
QStatusBar {{
    background-color: {t['bg_deep']};
    color: {t['text_muted']};
    font-size: 11px;
    border-top: 1px solid {t['border_subtle']};
}}

/* ─── Checkboxes ─── */
QCheckBox {{
    color: {t['text_primary']};
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {t['border_default']};
    border-radius: 3px;
    background: {t['bg_sunken']};
}}
QCheckBox::indicator:checked {{
    background: {t['accent']};
    border-color: {t['accent']};
}}

/* ─── Progress Bar ─── */
QProgressBar {{
    background-color: {t['bg_sunken']};
    border: none;
    border-radius: 2px;
    height: 4px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background-color: {t['accent']};
    border-radius: 2px;
}}

/* ─── Slider (for any sliders used) ─── */
QSlider::groove:horizontal {{
    height: 4px;
    background: {t['bg_sunken']};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 14px;
    height: 14px;
    background: {t['accent']};
    border-radius: 7px;
    margin: -5px 0;
}}
QSlider::handle:horizontal:hover {{
    background: {t['accent_hover']};
}}
QSlider::sub-page:horizontal {{
    background: {t['accent']};
    border-radius: 2px;
}}
"""
```

---

## Icons

**Approach:** Use [Material Symbols](https://fonts.google.com/icons) (Apache 2.0 licence — free to bundle) as SVG files. Tint them at render time by replacing the SVG `fill` colour with the current theme's relevant token.

**Required icons:**

| Name | Usage |
|------|-------|
| `add` | Add profile, add band |
| `delete` | Delete profile, delete band |
| `edit` | Rename |
| `folder_open` | Import |
| `save` | Save profile |
| `play_arrow` | Activate profile |
| `pause` | Bypass / deactivate |
| `settings` | Settings |
| `backup` | Backup |
| `restore` | Restore |
| `drag_handle` | Drag reorder handles |
| `chevron_right` / `expand_more` | Collapse/expand |
| `tune` | EQ / Filter indicator |
| `warning` | Warning banner |
| `check_circle` | Active / success indicator |
| `close` | Close / dismiss |
| `content_copy` | Copy raw config |
| `info` | Info tooltip trigger |

**Icon sizes:** 16px for toolbar/rows. 20px for buttons with icon+text. 24px for tray icon.

**SVG tinting:** Load SVG as string, replace `fill="#000000"` or `fill="currentColor"` with the target colour, render to `QPixmap` via `QSvgRenderer`. Build a helper `icon(name, colour)` function that handles this.

---

## Animation & Transitions

**Philosophy:** Fast and purposeful. Audio tools should feel snappy, not floaty.

| Element | Animation | Duration |
|---------|-----------|----------|
| Button hover state | None — instant | 0ms |
| EQ graph curve update | Instant redraw | 0ms |
| Profile switch (visual) | None | 0ms |
| Collapsible section expand | Height animation | 120ms ease-out |
| Settings dialog open | Fade-in | 80ms |
| Band handle drag | Real-time, no lag | 0ms |
| Warning banner appear | Slide-down | 100ms ease-out |

**Do not animate:** button presses, hover colours, text fields, dropdowns. These should always be instant.

---

## Settings Screen Design

Full-window dialog (800×600px, resizable). Left-side category list (Fusion-like settings structure):

- General
- Appearance (theme picker)
- EQ Defaults
- APO Integration
- Backup & Restore

**General category includes:**
- **Start with Windows** — toggle (default: ON). Writes/removes registry autostart entry at `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run`.
- **Start minimised to tray** — toggle (default: ON, only meaningful when Start with Windows is on). RUMBLE launches directly to tray with no window shown.
- Note displayed in General: *"RUMBLE always minimises to the system tray when the window is closed. To fully exit, use Quit in the tray menu."* Style this as an info banner (`info` colour, subdued).

**Theme picker:** Grid of 4 theme swatches. Each swatch is a 120×80px card showing the bg and accent colours with the theme name. Selected swatch has a `accent` border. Clicking applies the theme immediately (live preview).

---

## Raw Config View

Panel at bottom of editor (collapsible). `QPlainTextEdit`, read-only.

```
Font:       Cascadia Code / Consolas, 11px
Background: graph_bg (darkest, distinct from editor)
Text:       text_secondary
Border:     border_subtle
Radius:     radius_sm
Padding:    12px
```

Minimal syntax highlighting via `QSyntaxHighlighter`:
- Command keywords (`Filter`, `Preamp`, `Include`, etc.): `accent` colour
- Filter types (`PK`, `LS`, `HP`, etc.): `success` colour
- Numeric values: `text_primary`
- Comments (`#`): `text_muted`, italic
- `ON` / `OFF`: `warning` colour

`[Copy to clipboard]` ghost button top-right of this panel.

---

## Dependency Addition for Design

Add to `requirements.txt`:
```
PySide6
pyqtgraph
numpy
PyInstaller
```

No additional design libraries needed. All theming is custom QSS as specified above.

Bundle with application:
- `assets/fonts/Inter-Regular.ttf`
- `assets/fonts/Inter-Medium.ttf`
- `assets/fonts/Inter-SemiBold.ttf`
- `assets/icons/*.svg` (Material Symbols subset)
- `assets/themes/*.py` (the four theme dicts)
