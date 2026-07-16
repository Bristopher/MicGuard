# Future: same-monitor popups over exclusive-fullscreen games

**Captured:** 2026-07-16 (Bristopher: "id prefer if i could use same monitor
but yes lets use second monitor as fallback and dig into a first monitor
work arround" — after the R6 Siege Shift+F3 report)
**Status:** 💤 Parked — second-monitor fallback shipped (v1.7 test build);
this doc records the dig into same-monitor options.

## The wall

In TRUE exclusive fullscreen the game owns the display output; showing any
normal window on that monitor breaks exclusive mode and Windows minimizes
the game (exactly what MicGuard's popups did before the v1.6.1 suppression).
Tools like PowerToys "Always on Top" hit the same wall. There is no
supported way for a normal Win32 window to composite over it.

## Options table

| Option | Verdict |
|---|---|
| **Overlay injection** (hook the game's D3D/Vulkan present like Steam/Discord overlays) | ❌ Hard no. Requires DLL injection into the game process — R6 Siege runs BattlEye; injection risks a ban, plus it's miles outside a stdlib tray app. |
| **Windows z-band APIs** (the volume-flyout / Game Bar band that DOES draw over FSO games) | ❌ Undocumented/privileged (`CreateWindowInBand` needs a special signing level); Game Bar widgets get it via the XboxGameBar UWP platform — would mean shipping a separate UWP widget component. Interesting but a whole platform, parked. |
| **Fullscreen Optimizations (FSO) / flip-model** | ✅ The actual modern path. On Win 10/11, most games' "Fullscreen" is auto-converted to flip-model presentation (borderless-equivalent, same performance) and normal topmost windows draw fine over it. Siege reporting `QUNS_RUNNING_D3D_FULL_SCREEN` means it bypassed FSO (its own exclusive setting / Vulkan path). |
| **Borderless windowed in the game** | ✅ Identical result: with Siege set to **Borderless**, MicGuard's popups appear on the SAME monitor today, no code needed, and modern flip-model borderless has no meaningful performance penalty. |

## Practical answer (what Bristopher should do)

Set Siege Display Mode to **Borderless** — same monitor popups just work,
performance parity on Windows 11. Keep "Fullscreen" only if a measurable
input-latency difference matters more than the overlay; then the popups use
the second monitor (shipped fallback).

## If this ever gets built

The only sanctioned engineering route is the Game Bar widget: an XboxGameBar
UWP companion that renders the mixer as a widget (widgets composite over FSO
and many exclusive games). Separate packaged app, MSIX, store or sideload —
big scope, revisit only if the borderless answer stops being acceptable.

Sources from the dig: [Microsoft Q&A on drawing over fullscreen apps](https://learn.microsoft.com/en-us/answers/questions/5514066/how-would-i-put-an-application-(like-a-clock)-over),
[Blur Busters on Win11 exclusive fullscreen & z-bands](https://forums.blurbusters.com/viewtopic.php?t=13940),
[ResetEra: exclusive fullscreen mostly obsolete](https://www.resetera.com/threads/psa-in-most-cases-you-dont-need-exclusive-fullscreen-anymore.422177/),
[PresentMon overlay vs legacy exclusive fullscreen](https://github.com/GameTechDev/PresentMon/issues/212).
