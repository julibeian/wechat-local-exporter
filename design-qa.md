# Star 横幅位置与选择列表伸展：视觉 QA

- source visual truth: `C:\Users\Lenovo\AppData\Local\Temp\codex-clipboard-d9b2446a-2d78-466c-933e-39d2851ab5cc.png`
- implementation, Star visible: `C:\Users\Lenovo\Documents\ChatGPT\家教\tmp\design-qa\star-visible-export-bottom-final.png`
- implementation, Star dismissed: `C:\Users\Lenovo\Documents\ChatGPT\家教\tmp\design-qa\star-hidden-expanded-list-final.png`
- normalized comparison: `C:\Users\Lenovo\Documents\ChatGPT\家教\tmp\design-qa\source-vs-implementation-final.png`
- viewport: Windows maximized desktop window
- source pixels: 2559 × 1494
- implementation capture: 2560 × 1529, including 35 px system title bar
- normalized implementation: crop 2559 × 1494 from y=35; no density resampling
- state: Star visible in the export-area footer, followed by Star dismissed

## Full-view comparison evidence

The requested red-box region below the export action buttons now contains the full-width Star banner. The connection and chooser sections retain the existing product typography, colors, borders, and spacing. The status/progress row remains outside and below the export panel.

After the close button is activated, the banner and its grid padding collapse. The vertical pane sash moves downward by the reclaimed height, so the chooser displays additional group/contact rows while the export controls remain fully visible.

## Focused region comparison evidence

- Export footer: Star text, blue action, dismiss action, and right-aligned close affordance are unchanged; only the host location changes from above section 1 to below the export actions.
- Chooser height: the visible-Star state preserves the original sash height when space permits. In the dismissed state, the chooser bottom moves down by the banner's reclaimed height and exposes the following P/X rows in the preview data.
- The focused regions were inspected at the same maximized viewport because the requested behavior depends on the relationship between the two panes rather than an isolated component.

## Required fidelity surfaces

- Fonts and typography: existing Microsoft YaHei UI hierarchy, weights, and sizes are preserved.
- Spacing and layout rhythm: banner is placed after the export action row with a small top gap; closing it removes both banner and gap. No persistent empty placeholder remains.
- Colors and visual tokens: existing blue/white Star banner and neutral application palette are preserved.
- Image quality and asset fidelity: no raster image assets are used or replaced in this desktop UI.
- Copy and content: existing Star invitation, action labels, estimates, and export copy are unchanged.

## Findings

No actionable P0, P1, or P2 differences remain for the requested change.

## Comparison history

1. Initial implementation placed the banner correctly but recalculated both pane sizes from requested heights, which shortened the chooser in the visible-Star state more than necessary (P2).
2. The layout was revised to preserve the existing sash while the banner is visible, shrink it only when required for minimum export height, and transfer only the dismissed banner's measured height to the chooser.
3. Post-fix evidence shows the banner in the requested export footer and additional chooser rows immediately after dismissal.

## Interactions checked

- Maximized-window layout.
- Star close button.
- Immediate chooser-height expansion after close.
- Export buttons and status row remain visible.
- Browser console: not applicable to the native Tkinter desktop application.

## Follow-up polish

No P3 follow-up is required for this scoped change.

final result: passed
