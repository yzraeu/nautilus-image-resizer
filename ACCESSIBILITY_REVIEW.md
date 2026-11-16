# Accessibility review — Nautilus Image Resizer

Audit of `nautilus-image-resizer/image_resizer.py` (GTK 4 + libadwaita dialog) and
`nautilus/image_resizer_extension.py` (Nautilus menu provider) against the
accessibility requirements GNOME publishes. Each finding was located in the source,
fixed, and the fix was checked against the running app's accessibility tree; the
manual verification log records what was observed and what could not be observed on
this machine.

## Sources used

- GNOME HIG — Accessibility: <https://developer.gnome.org/hig/guidelines/accessibility.html>
  (descriptive accessible names; test high-contrast mode, large text mode, keyboard
  navigation, screen reader, on-screen keyboard).
- GNOME HIG — Keyboard: <https://developer.gnome.org/hig/guidelines/keyboard.html>
  (every part reachable and operable by keyboard; Tab follows the widget tree; arrow
  keys move directionally where possible; standard keys: Return activates, Space
  toggles, Esc closes transient containers).
- GNOME HIG — Standard Keyboard Shortcuts: <https://developer.gnome.org/hig/reference/keyboard>
  (Ctrl+W closes).
- GNOME Developer docs — Coding Guidelines for Supporting Accessibility:
  <https://developer.gnome.org/documentation/guidelines/accessibility/coding-guidelines.html>
  (`GTK_ACCESSIBLE_PROPERTY_LABEL` on components without a short string;
  `DESCRIPTION` where a description is needed; label↔control relations; custom
  widgets must support accessibility; don't break inherited accessibility).
- GNOME Developer docs — Making Custom Components Accessible:
  <https://developer.gnome.org/documentation/guidelines/accessibility/custom-widgets.html>
  (assign a role; update properties when content changes; update states when state
  changes).
- WCAG 2.1 AA (text contrast) and WCAG 2.2 SC 2.5.8 (target size ≥ 24 CSS px) as the
  numeric standards the GNOME guidance tracks.

## Requirements checklist

| Requirement | Source | Result | Evidence / change |
| --- | --- | --- | --- |
| Every element has a descriptive accessible name | HIG Accessibility | Pass (was fail) | `PreviewDeck` (`set_accessible_role(IMG)`, `set_label`) → `Image preview: <file>, plus N more images`; `menu_button.update_property(LABEL, "Main Menu")`; `chain` named `Lock aspect ratio`; `crop_switch` named `Enable crop`. Seen in the AT-SPI dump below. |
| State is not encoded in an accessible name | dev docs (coding guidelines) | Pass (was fail) | `set_chain_state` no longer rewrites the name; pressed state + tooltip carry the lock state. |
| Components without a short string get `LABEL`; regions get `DESCRIPTION` | dev docs (coding guidelines) | Pass (was fail) | `crop_area.update_property(LABEL, DESCRIPTION)`; preset buttons keep their labels. |
| Custom widgets declare a role and update properties | dev docs (custom components) | Pass (was fail) | `PreviewDeck` → `IMG` + label updated in `apply_source`; crop overlay → `GROUP` + label/description at construction. |
| Inherited accessibility is not broken | dev docs (coding guidelines) | Pass | No `Gtk.Accessible` container was made insensitive or replaced by a custom widget; all custom drawing sits in `Gtk.Overlay` siblings of standard widgets. |
| Every part reachable and operable by keyboard | HIG Keyboard | Pass (was fail) | Crop was drag-only. `crop_area.set_focusable()` + `Gtk.EventControllerKey`: Arrow moves (1 % of the image dimension per press), Shift+Arrow resizes (10 %), 16 px minimum, aspect-aware with a ratio preset. |
| Tab order follows the widget tree | HIG Keyboard | Pass | Header menu → preview region (only while crop is armed) → view switcher → settings → crop section → output rows → Cancel/Reset/Resize. |
| Return activates the primary action | HIG Keyboard | Pass (was fail) | `win.set_default_widget(resize)`; observed default widget = `Resize`. |
| Space toggles the focused control | HIG Keyboard | Pass | All toggles are stock `Gtk.Switch` / `Gtk.ToggleButton`; the switches were exercised via their accessible `toggle` action. |
| Esc closes the transient window | HIG Keyboard | Pass (was fail) | Window `EventControllerKey`; observed `close-request` on Esc. |
| Ctrl+W closes | HIG Keyboard Shortcuts | Pass (was fail) | `win.close` action + `app.set_accels_for_action("win.close", ["<Control>w"])`. |
| Grouped controls behave as a group | HIG (keyboard/pointer) | Pass (was fail) | Ratio presets grouped with `Gtk.ToggleButton.set_group`; observed exactly one active after switching presets. |
| Screen reader: elements read aloud with accurate names | HIG Accessibility | Pass (was fail) | AT-SPI dump below. |
| Screen reader: dynamic updates announced | HIG Accessibility | Pass (code path) | `Gtk.Accessible.announce` at `LOW` priority for `Calculating…`, estimate results/failures and crop geometry; crop announcements debounced 450 ms so auto-repeat coalesces. Not audibly verified (no screen reader running here). |
| Failures are surfaced, not swallowed | HIG (feedback) | Pass (was fail) | All-success still closes; on failure the window stays open with a `Gtk.AlertDialog`. Observed message/detail below. |
| High-contrast rendering | HIG Accessibility | Pass (measured) | No literal colours in text; glyphs use `get_color()`; crop overlay is a black+white double stroke over the photo. Application starts and lays out under `GTK_THEME=HighContrast`. |
| Large text mode | HIG Accessibility | Pass (measured) | At `text-scaling-factor 2.0` the settings content needs 619 px and the window grows to 619 px on a 1920 px monitor — no clipping. |
| On-screen keyboard | HIG Accessibility | Not exercised | Every text input is a stock `Adw.EntryRow`; OSK is disabled in this session, so it could not be driven. |
| Pointer/touch target size | WCAG 2.2 SC 2.5.8 | Pass (was marginal) | Ratio presets `set_size_request(64, 48)`. |
| Strings are translatable | GNOME app conventions | Pass (was fail) | `gettext` domain `nautilus-image-resizer` in both entry points; every user-visible string wrapped, `{}` templates for formatted messages. The default filename suffix `_resized` stays untranslated so it remains filename-safe. |

## Findings and fixes

| # | Finding (before) | Fix |
| --- | --- | --- |
| F1 | Crop selectable with the mouse only (`Gtk.GestureDrag`) | Focusable region + key controller; new unit-tested helpers `crop_move_rect` / `crop_resize_rect`; starting rect on first arrow press; thicker outline while focused |
| F2 | `PreviewDeck` was a silent generic node | Role `IMG`, name from file and selection count |
| F3 | Crop overlay generic and unnamed | Role `GROUP`, label + keyboard description |
| F4 | Lock toggle encoded state in its name | Static name, description, state via pressed |
| F5 | `Gtk.Switch` widgets had no accessible label | `Enable crop` label; overwrite row → `Adw.SwitchRow` |
| F6 | Icon-only menu button named by tooltip only | Explicit `Main Menu` label (tooltip kept) |
| F7 | Presets hand-rolled radio behaviour | `Gtk.ToggleButton.set_group`; manual unsetting deleted |
| F8 | Live status/estimate text invisible to AT | `Gtk.Accessible.announce` (`LOW`), crop text debounced |
| F9 | Resize results discarded, window always closed | `Gtk.AlertDialog` naming failed files and reasons |
| F10 | Crop outline was white-only | Black+white double stroke, wider on focus |
| F11 | Smallest targets ~26×18 glyph buttons | `64×48` presets |
| F12 | No gettext | `_()` wrapping in both entry points |
| F13 | No Esc / Return / Ctrl+W | Default widget, window key controller, `win.close` accel |

## Manual verification

Runtime: GTK 4.24.1, libadwaita 1.10.0, GNOME Wayland session. The app was driven
in-process through its own widgets and controllers (Wayland offers no input-synthesis
tooling here), and its accessibility tree was read over AT-SPI with `pyatspi`.

Accessibility tree with two images selected — the nodes that previously read as
generic or unnamed:

```
image: 'Image preview: a11y-photo.png, plus 1 more images'
grouping: 'Crop selection area' — Arrow keys move the crop region; Shift+arrow keys resize it
switch: 'Enable crop'
switch: 'Overwrite original files'
button: 'Main Menu'
page tab: 'Percentage' / 'Dimensions' (pressed) / 'File Size'
button: 'Cancel' / 'Reset' / 'Resize'
```

Keyboard crop, real 800×600 image loaded, crop armed through the switch:

| Action | Observed |
| --- | --- |
| Shift+Left ×3 (resize, 10 % per press) | `Crop 560 × 600 px at 0, 0` |
| Right (move, 1 % per press) | `Crop 560 × 600 px at 8, 0` |
| Down | no change — the region already spans the full height, so the move clamps |
| Preset switch Original → Free | exactly one preset active at a time |
| Return | default widget is `Resize` |

Failure reporting, launched with `/tmp/a11y-photo.png /etc/hostname`, Resize pressed:

```
dialog:  '1 of 2 images could not be resized'
detail:  'hostname — not a readable image'
buttons: 'Close'
window still visible; Cancel/Reset/Resize re-enabled, Resize label restored
```

Escape: emitting Esc on the window produced `close-request` — the window closes from
any focus position.

Layout measurements:

| Setting | Content minimum width | Window width | Monitor |
| --- | --- | --- | --- |
| Default | 399 px | 560 px | 1920×1200 |
| `text-scaling-factor 2.0` | 619 px | 619 px | 1920×1200 |
| `GTK_THEME=HighContrast` | 421 px | 560 px | 1920×1200 |

Unit suite: `python3 -m unittest discover -s tests -v` — all tests pass, including the
nine keyboard-crop tests (translation, clamping, anchor preservation at the image
edge, aspect preservation at the edge, height-driven resize, 16 px minimum).

Not verified on this machine: audible screen-reader output (`Gtk.Accessible.announce`
runs but no screen reader was running), on-screen keyboard input, and visual capture
of high-contrast/large-text rendering (the session denies D-Bus screenshots, so those
rows rest on the measurements above plus the absence of literal colours in the code).

## Known limitations

- Translation-ready only: no `.pot`/`.po` catalogs ship, so the UI stays English until
  one is installed.
- The preview deck is a single aggregate `IMG` node; individual selected files are not
  separately navigable in the accessibility tree (the caption and window subtitle
  carry the count).
- Crop announcements are `LOW` priority and debounced: they are spoken when the reader
  is idle, and only the settled geometry is announced.
- Return activates *Resize* even while focus is in the suffix entry — standard GTK
  dialog default-button behaviour, kept deliberately.
- The vertical paned splitter between preview and settings is a stock `Gtk.Paned`; its
  keyboard adjustment uses the platform default.
