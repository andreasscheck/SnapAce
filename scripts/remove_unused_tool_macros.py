#!/usr/bin/env python3
"""Remove the T4-T31 tool-change stub macros from Snapmaker's stock fluidd.cfg.

The Snapmaker U1 only has 4 physical extruders. T0-T3 aren't macros in
fluidd.cfg at all - Klipper registers them natively from the 4
[extruder]/[extruderN] sections. Stock still ships 28 extra
`[gcode_macro Tn]` stubs (n=4..31), each just calling
SWITCH_OF_EXTENDED_EXTRUDER, apparently so a stray T4+ (e.g. from AFC/MMU
tooling this printer doesn't have) doesn't error out. Fluidd's Tools panel
renders one button per registered Tn command, so these 28 stubs show up as
28 dead buttons.

Usage: remove_unused_tool_macros.py <input fluidd.cfg> <output path>
"""
import re
import sys

PATTERN = re.compile(
    r'\[gcode_macro T(?:[4-9]|[12][0-9]|3[01])\]\n'
    r'gcode:\n'
    r'    SWITCH_OF_EXTENDED_EXTRUDER INDEX=\d+\n\n'
)
EXPECTED_COUNT = 28


def main(argv):
    if len(argv) != 3:
        print(f"Usage: {argv[0]} <input fluidd.cfg> <output path>", file=sys.stderr)
        return 1

    with open(argv[1]) as f:
        text = f.read()

    new_text, count = PATTERN.subn('', text)
    if count != EXPECTED_COUNT:
        print(f"Expected to remove {EXPECTED_COUNT} macro blocks, removed "
              f"{count} - stock fluidd.cfg may have changed, check the "
              "pattern before proceeding.", file=sys.stderr)
        return 1

    with open(argv[2], 'w') as f:
        f.write(new_text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
