#!/usr/bin/env python3
"""
focus_diagnostic.py
Diagnostic script to test ALL focus control methods on Sony A7 III via USB.
Run with the camera connected, lens mounted, USB mode = PC Remote.

Usage:
    python3 focus_diagnostic.py          # read-only diagnostics
    python3 focus_diagnostic.py --move   # also test actual focus movement
"""

import argparse
import sys
import time
from datetime import datetime

try:
    import gphoto2 as gp
except ImportError:
    print("ERROR: python-gphoto2 not installed. Run: pip install gphoto2")
    sys.exit(1)

FOCUS_WIDGETS = [
    "manualfocus", "focalposition", "autofocus", "focusmode",
    "focusarea", "focusmagnifier", "focusmagnifierexit",
    "focusmagnifiersetting", "spotfocusarea",
    "manualfocusdrive", "nearfocus", "farfocus",
]

LOG = []


def log(msg: str):
    print(msg)
    LOG.append(msg)


def save_results():
    fname = "focus_diagnostic_results.txt"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(f"Focus Diagnostic Results — {datetime.now()}\n")
        f.write("=" * 60 + "\n\n")
        f.write("\n".join(LOG))
    log(f"\n>>> Results saved to {fname}")


def walk_config(widget, results, prefix=""):
    name = widget.get_name()
    wtype = widget.get_type()
    path = f"{prefix}/{name}" if prefix else name
    type_names = {
        gp.GP_WIDGET_WINDOW: "WINDOW", gp.GP_WIDGET_SECTION: "SECTION",
        gp.GP_WIDGET_TEXT: "TEXT", gp.GP_WIDGET_RANGE: "RANGE",
        gp.GP_WIDGET_TOGGLE: "TOGGLE", gp.GP_WIDGET_RADIO: "RADIO",
        gp.GP_WIDGET_MENU: "MENU", gp.GP_WIDGET_DATE: "DATE",
        gp.GP_WIDGET_BUTTON: "BUTTON",
    }
    if wtype not in (gp.GP_WIDGET_SECTION, gp.GP_WIDGET_WINDOW):
        try:
            val = widget.get_value()
        except gp.GPhoto2Error:
            val = "<error>"
        type_str = type_names.get(wtype, f"UNKNOWN({wtype})")
        info = {"path": path, "name": name, "type": type_str, "value": val, "widget": widget}
        if wtype == gp.GP_WIDGET_RANGE:
            try:
                lo, hi, step = widget.get_range()
                info["range"] = (lo, hi, step)
            except gp.GPhoto2Error:
                info["range"] = None
        if wtype in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU):
            try:
                info["choices"] = [widget.get_choice(i) for i in range(widget.count_choices())]
            except gp.GPhoto2Error:
                info["choices"] = []
        try:
            info["readonly"] = widget.get_readonly()
        except Exception:
            info["readonly"] = None
        results.append(info)
    for i in range(widget.count_children()):
        walk_config(widget.get_child(i), results, path)


# ═══════════════════════════════════════════════
# TEST 1: List ALL config widgets
# ═══════════════════════════════════════════════
def test_1_list_all_widgets(camera, context):
    log("\n" + "=" * 60)
    log("TEST 1: List ALL config widgets")
    log("=" * 60)
    config = camera.get_config(context)
    results = []
    walk_config(config, results)
    log(f"\nTotal widgets found: {len(results)}")

    focus_kw = ["focus", "focal", "af", "mf", "manual", "lens", "near", "far"]
    log("\n--- FOCUS-RELATED WIDGETS ---")
    found_any = False
    for info in results:
        nl = info["name"].lower()
        pl = info["path"].lower()
        if any(kw in nl or kw in pl for kw in focus_kw):
            found_any = True
            line = f"  {info['path']:50s} type={info['type']:6s}  value={info['value']}"
            if info.get("range"):
                line += f"  range={info['range']}"
            if info.get("choices"):
                line += f"  choices={info['choices']}"
            if info.get("readonly"):
                line += "  [READONLY]"
            log(line)
    if not found_any:
        log("  (none found!)")

    log("\n--- ALL WIDGETS (complete dump) ---")
    for info in results:
        line = f"  {info['path']:50s} type={info['type']:6s}  value={info['value']}"
        if info.get("range"):
            line += f"  range={info['range']}"
        if info.get("readonly"):
            line += "  [READONLY]"
        log(line)
    return results


# ═══════════════════════════════════════════════
# TEST 2: get_single_config for each focus widget
# ═══════════════════════════════════════════════
def test_2_get_single_config(camera, context):
    log("\n" + "=" * 60)
    log("TEST 2: get_single_config for known focus widget names")
    log("=" * 60)
    for name in FOCUS_WIDGETS:
        try:
            widget = camera.get_single_config(name, context)
            wtype = widget.get_type()
            val = widget.get_value()
            log(f"  ✓ {name:30s} type={wtype}  value={val}")
            if wtype == gp.GP_WIDGET_RANGE:
                try:
                    lo, hi, step = widget.get_range()
                    log(f"    range: [{lo}, {hi}] step={step}")
                except gp.GPhoto2Error:
                    pass
        except gp.GPhoto2Error as e:
            log(f"  ✗ {name:30s} — {e}")


# ═══════════════════════════════════════════════
# TEST 3: Focus mode and area
# ═══════════════════════════════════════════════
def test_3_focus_mode(camera, context):
    log("\n" + "=" * 60)
    log("TEST 3: Focus mode settings")
    log("=" * 60)
    for name in ["focusmode", "focusarea"]:
        try:
            widget = camera.get_single_config(name, context)
            val = widget.get_value()
            log(f"  {name}: current = {val}")
            wtype = widget.get_type()
            if wtype in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU):
                choices = [widget.get_choice(i) for i in range(widget.count_choices())]
                log(f"    available: {choices}")
        except gp.GPhoto2Error as e:
            log(f"  {name}: not available via get_single_config — {e}")
            try:
                config = camera.get_config(context)
                widget = config.get_child_by_name(name)
                val = widget.get_value()
                log(f"  {name} (via full config): current = {val}")
            except gp.GPhoto2Error:
                log(f"  {name} (via full config): also failed")


# ═══════════════════════════════════════════════
# TEST 4: manualfocus read/write
# ═══════════════════════════════════════════════
def test_4_manualfocus_readwrite(camera, context):
    log("\n" + "=" * 60)
    log("TEST 4: manualfocus widget read/write test")
    log("=" * 60)

    log("\n  a) get_single_config('manualfocus'):")
    try:
        widget = camera.get_single_config("manualfocus", context)
        val = widget.get_value()
        wtype = widget.get_type()
        log(f"     value={val}  type={'RANGE' if wtype == gp.GP_WIDGET_RANGE else wtype}")
        if wtype == gp.GP_WIDGET_RANGE:
            lo, hi, step = widget.get_range()
            log(f"     range=[{lo}, {hi}] step={step}")
        log(f"     readonly={widget.get_readonly()}")
    except gp.GPhoto2Error as e:
        log(f"     FAILED: {e}")

    log("\n  b) get_config → get_child_by_name('manualfocus'):")
    try:
        config = camera.get_config(context)
        widget = config.get_child_by_name("manualfocus")
        log(f"     value={widget.get_value()}")
    except gp.GPhoto2Error as e:
        log(f"     FAILED: {e}")

    log("\n  c) set_single_config('manualfocus', 0.0) [no-op]:")
    try:
        widget = camera.get_single_config("manualfocus", context)
        widget.set_value(0.0)
        camera.set_single_config("manualfocus", widget, context)
        log("     OK — no error")
    except gp.GPhoto2Error as e:
        log(f"     FAILED: {e}")

    log("\n  d) set_config (full tree, manualfocus=0.0):")
    try:
        config = camera.get_config(context)
        widget = config.get_child_by_name("manualfocus")
        widget.set_value(0.0)
        camera.set_config(config, context)
        log("     OK — no error")
    except gp.GPhoto2Error as e:
        log(f"     FAILED: {e}")


# ═══════════════════════════════════════════════
# Helper: read focalposition via all methods
# ═══════════════════════════════════════════════
def read_focal_position(camera, context):
    for method_name, read_fn in [
        ("get_single_config", lambda: camera.get_single_config("focalposition", context)),
        ("full config tree", lambda: camera.get_config(context).get_child_by_name("focalposition")),
    ]:
        try:
            w = read_fn()
            return int(float(w.get_value())), method_name
        except (gp.GPhoto2Error, ValueError):
            pass
    return None, "none"


# ═══════════════════════════════════════════════
# TEST 5: Actual focus movement
# ═══════════════════════════════════════════════
def test_5_move_focus(camera, context):
    log("\n" + "=" * 60)
    log("TEST 5: ACTUAL FOCUS MOVEMENT — watch the lens!")
    log("=" * 60)

    pos_before, method = read_focal_position(camera, context)
    log(f"\n  Initial focalposition: {pos_before} (via {method})")

    test_values = [
        (-1.0, "small near"),  (-3.0, "medium near"), (-7.0, "large near"),
        (1.0, "small far"),    (3.0, "medium far"),    (7.0, "large far"),
    ]

    for value, desc in test_values:
        log(f"\n  >>> manualfocus = {value} ({desc})")
        ok = False
        try:
            widget = camera.get_single_config("manualfocus", context)
            widget.set_value(value)
            camera.set_single_config("manualfocus", widget, context)
            log("      set_single_config: OK")
            ok = True
        except gp.GPhoto2Error as e:
            log(f"      set_single_config FAILED: {e}")

        if not ok:
            try:
                config = camera.get_config(context)
                widget = config.get_child_by_name("manualfocus")
                widget.set_value(value)
                camera.set_config(config, context)
                log("      set_config (full): OK")
                ok = True
            except gp.GPhoto2Error as e2:
                log(f"      set_config (full) FAILED: {e2}")
                continue

        time.sleep(0.5)
        pos_after, _ = read_focal_position(camera, context)
        log(f"      focalposition after: {pos_after}")
        if pos_before is not None and pos_after is not None:
            if pos_after != pos_before:
                log(f"      ✓ MOVEMENT: {pos_before} → {pos_after}")
            else:
                log(f"      ✗ NO MOVEMENT (still {pos_before})")
        pos_before = pos_after


# ═══════════════════════════════════════════════
# TEST 6: Autofocus trigger
# ═══════════════════════════════════════════════
def test_6_autofocus(camera, context):
    log("\n" + "=" * 60)
    log("TEST 6: Autofocus trigger")
    log("=" * 60)

    try:
        w = camera.get_single_config("focusmode", context)
        log(f"  Current focusmode: {w.get_value()}")
    except gp.GPhoto2Error:
        log("  Could not read focusmode")

    log("\n  Triggering autofocus (autofocus=2)...")
    try:
        w = camera.get_single_config("autofocus", context)
        log(f"  autofocus current value: {w.get_value()}")
        w.set_value(2)
        camera.set_single_config("autofocus", w, context)
        log("  ✓ autofocus=2 sent")
        time.sleep(2.0)
        w = camera.get_single_config("autofocus", context)
        w.set_value(1)
        camera.set_single_config("autofocus", w, context)
        log("  autofocus=1 (release) sent")
    except gp.GPhoto2Error as e:
        log(f"  ✗ autofocus FAILED: {e}")


# ═══════════════════════════════════════════════
# TEST 7: Focus mode changes
# ═══════════════════════════════════════════════
def test_7_focus_mode_change(camera, context):
    log("\n" + "=" * 60)
    log("TEST 7: Focus mode change test")
    log("=" * 60)

    try:
        widget = camera.get_single_config("focusmode", context)
        original = widget.get_value()
        log(f"  Current focusmode: {original}")
        choices = []
        wtype = widget.get_type()
        if wtype in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU):
            choices = [widget.get_choice(i) for i in range(widget.count_choices())]
            log(f"  Available modes: {choices}")

        for mode in ["DMF", "Manual", "AF-S", "AF-C"]:
            if mode in choices:
                log(f"\n  Trying focusmode = '{mode}'...")
                try:
                    widget = camera.get_single_config("focusmode", context)
                    widget.set_value(mode)
                    camera.set_single_config("focusmode", widget, context)
                    time.sleep(0.5)
                    widget = camera.get_single_config("focusmode", context)
                    new_val = widget.get_value()
                    log(f"  {'✓' if new_val == mode else '?'} focusmode now: '{new_val}'")
                except gp.GPhoto2Error as e:
                    log(f"  ✗ Failed: {e}")

        log(f"\n  Restoring focusmode = '{original}'...")
        try:
            widget = camera.get_single_config("focusmode", context)
            widget.set_value(original)
            camera.set_single_config("focusmode", widget, context)
            log("  ✓ Restored")
        except gp.GPhoto2Error as e:
            log(f"  ✗ Could not restore: {e}")

    except gp.GPhoto2Error as e:
        log(f"  ✗ Cannot access focusmode: {e}")


# ═══════════════════════════════════════════════
# TEST 8: DMF mode + manual focus
# ═══════════════════════════════════════════════
def test_8_dmf_manual_focus(camera, context):
    log("\n" + "=" * 60)
    log("TEST 8: DMF mode + manual focus (electronic MF)")
    log("=" * 60)

    original_mode = None
    try:
        widget = camera.get_single_config("focusmode", context)
        original_mode = widget.get_value()
        choices = [widget.get_choice(i) for i in range(widget.count_choices())]
        log(f"  Current: {original_mode}, Available: {choices}")
    except gp.GPhoto2Error:
        log("  Cannot read focusmode — skipping")
        return

    if "DMF" not in choices:
        log("  DMF not available — skipping")
        return

    try:
        widget = camera.get_single_config("focusmode", context)
        widget.set_value("DMF")
        camera.set_single_config("focusmode", widget, context)
        time.sleep(1.0)
        widget = camera.get_single_config("focusmode", context)
        log(f"  focusmode now: {widget.get_value()}")
    except gp.GPhoto2Error as e:
        log(f"  Cannot set DMF: {e}")
        return

    pos_before, _ = read_focal_position(camera, context)
    log(f"  focalposition before: {pos_before}")

    for val, desc in [(-3.0, "near"), (3.0, "far")]:
        log(f"\n  manualfocus={val} ({desc}) in DMF mode...")
        try:
            widget = camera.get_single_config("manualfocus", context)
            widget.set_value(val)
            camera.set_single_config("manualfocus", widget, context)
            log("  Command sent OK")
            time.sleep(0.5)
            pos, _ = read_focal_position(camera, context)
            log(f"  focalposition after: {pos}")
            if pos_before is not None and pos is not None and pos != pos_before:
                log("  ✓ MOVEMENT IN DMF MODE!")
            pos_before = pos
        except gp.GPhoto2Error as e:
            log(f"  FAILED: {e}")

    if original_mode:
        try:
            widget = camera.get_single_config("focusmode", context)
            widget.set_value(original_mode)
            camera.set_single_config("focusmode", widget, context)
            log(f"\n  Restored focusmode = '{original_mode}'")
        except gp.GPhoto2Error as e:
            log(f"\n  Could not restore: {e}")


# ═══════════════════════════════════════════════
# TEST 9: Status flags
# ═══════════════════════════════════════════════
def test_9_mf_enable_status(camera, context):
    log("\n" + "=" * 60)
    log("TEST 9: MF enable status and related flags")
    log("=" * 60)
    try:
        config = camera.get_config(context)
        results = []
        walk_config(config, results)
        kws = ["enable", "status", "adjust", "mfassist", "hold"]
        for info in results:
            if any(kw in info["name"].lower() for kw in kws):
                log(f"  {info['path']:50s} = {info['value']}  ({info['type']})")
    except gp.GPhoto2Error as e:
        log(f"  Cannot read config: {e}")


# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Sony A7 III focus diagnostic")
    parser.add_argument("--move", action="store_true",
                        help="Also test actual focus movement (Tests 5-8)")
    args = parser.parse_args()

    log("=" * 60)
    log("SONY A7 III FOCUS DIAGNOSTIC")
    log(f"Date: {datetime.now()}")
    log(f"gphoto2 version: {gp.gp_library_version(gp.GP_VERSION_SHORT)}")
    log("=" * 60)

    import platform
    if platform.system() == "Darwin":
        import subprocess
        log("\nKilling PTPCamera daemon (macOS)...")
        for _ in range(3):
            subprocess.run(["killall", "-9", "PTPCamera"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.5)
        time.sleep(1)

    log("\nConnecting to camera...")
    context = gp.Context()
    camera = gp.Camera()
    try:
        camera.init(context)
    except gp.GPhoto2Error as e:
        log(f"FATAL: Cannot connect: {e}")
        log("\nCheck: camera ON, USB, mode = PC Remote")
        save_results()
        sys.exit(1)

    try:
        summary = camera.get_summary(context)
        log(f"Connected: {str(summary)[:100]}")
    except gp.GPhoto2Error:
        log("Connected (summary unavailable)")

    try:
        all_widgets = test_1_list_all_widgets(camera, context)
        test_2_get_single_config(camera, context)
        test_3_focus_mode(camera, context)
        test_4_manualfocus_readwrite(camera, context)
        test_9_mf_enable_status(camera, context)

        if args.move:
            log("\n" + "#" * 60)
            log("# MOVEMENT TESTS — watch the lens!")
            log("#" * 60)
            test_5_move_focus(camera, context)
            test_6_autofocus(camera, context)
            test_7_focus_mode_change(camera, context)
            test_8_dmf_manual_focus(camera, context)
        else:
            log("\n" + "-" * 60)
            log("Skipped movement tests. Run with --move to test actual movement.")
            log("-" * 60)

        # Summary
        log("\n" + "=" * 60)
        log("SUMMARY")
        log("=" * 60)
        widget_names = [i["name"] for i in all_widgets] if all_widgets else []
        has_mf = "manualfocus" in widget_names
        has_fp = "focalposition" in widget_names
        log(f"  manualfocus widget:   {'FOUND' if has_mf else 'NOT FOUND'}")
        log(f"  focalposition widget: {'FOUND' if has_fp else 'NOT FOUND'}")

        if not args.move:
            log("\n  >>> Run with --move to test actual lens movement <<<")
    finally:
        camera.exit(context)
        log("\nCamera disconnected.")

    save_results()


if __name__ == "__main__":
    main()
