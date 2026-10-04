---
title: Choosing a 49-inch PbP monitor for coding and Mac capture
description: Compare 49-inch monitors for Windows and Mac Mini coding, with HDMI pass-through capture for OBS recordings.
date: '2026-09-04'
weight: 100
type: docs
id: 881d4eb7f61ae72b
destination: web-clips
stage: published
created_by: idea catcher
tags:
- tech-note
- home-lab
- video-production
source: https://gemini.google.com/app/881d4eb7f61ae72b
source_file: clippings/20260904-3fe80f-connecting-two-pc-to-ultrawide.md
original_filename: Connecting two pc to ultrawide.md
llm:
  profile: clippings
  backend: openai
  model: gpt-6-sol
  prompt_version: ai-chat-2
---
## 📝 Summary

- Picture-by-Picture (PbP) can show the Windows PC and Mac Mini M1 side by side; a 5120×1440 monitor gives each computer a 2560×1440 area.
- The existing Eizo EV2450 screens are 1920×1080 each. A 49-inch Dual-QHD PbP setup would provide more pixels per computer, though text appears smaller at 100% scaling.
- For mostly VS Code and web browsing, the chat favored LCD over QD-OLED because of text rendering and static-interface concerns.
- The Dell U4924DW, LG 49WQ95C-W and Samsung G91F were compared, but no monitor was selected.
- The proposed Mac Mini → UGREEN capture-card HDMI loop-out → monitor path also sends a USB capture feed to OBS on Windows. The card’s actual USB capture formats and loop-out refresh rates remain unverified.

## ✅ Decisions

- Use PbP, not Picture-in-Picture, to display the two computers simultaneously.
- Prioritize coding readability over KVM features.
- Keep the ability to record the Mac Mini in OBS on the Windows PC through the existing UGREEN capture card.

## 🔀 Options

- Dell UltraSharp U4924DW: 49-inch 5120×1440 IPS Black, favored for text contrast and office use; limited to 60Hz.
- LG 49WQ95C-W: 49-inch 5120×1440 Nano IPS with up to 144Hz for smoother scrolling; lower stated contrast than the Dell, and its PbP refresh rate needs checking.
- Samsung Odyssey G9 G91F (LS49FG916EUXEN): non-OLED VA LCD with a stated 144Hz maximum; avoids OLED-specific text fringing, but was considered less uniform at wide viewing angles than IPS.
- Samsung Odyssey OLED G9 (G95SC/G93SC): strong gaming and media option, but its QD-OLED subpixel layout and static-UI burn-in risk make it less attractive for primarily coding.
- Keep the two Eizo EV2450 displays: familiar text presentation at 1920×1080 per screen, but less resolution per computer and a center bezel.

## ❓ Open Questions

- Which monitor offers the best acceptable balance of text readability, PbP refresh rate and price for this particular desk setup?
- What resolutions and refresh rates does the UGREEN card identified by ASIN B0D4LV836Z actually support on its HDMI loop-out and, separately, its USB capture output?
- Will the Mac Mini M1, capture card and chosen monitor negotiate 2560×1440 at the desired refresh rate in PbP mode?
- What resolution and frame rate will OBS actually receive from the capture card, and is that sufficient for readable code in the intended YouTube recordings?

## 🖥️ Display layout and readability

PbP requires two independent video inputs and a monitor that explicitly supports the feature. On a 49-inch 5120×1440 display split equally, each computer uses a 2560×1440, 16:9 area. Enable PbP in the monitor menu, assign its inputs to the two sides, and check the resolution reported by each operating system.

| Display | Resolution per computer | Approximate pixel density | Coding implication |
| --- | --- | --- | --- |
| Eizo FlexScan EV2450, two screens | 1920×1080 each | 93 PPI | Larger text at 100% scaling; conventional IPS text rendering. |
| 49-inch 5120×1440 monitor in PbP | 2560×1440 each | 109 PPI | More workspace; text is roughly 15% smaller at 100% scaling. |

The combined pixel count rises by approximately 78% when moving from two 1920×1080 screens to 5120×1440. Font size need not stay at its default: VS Code font size, browser zoom and OS display scaling can be adjusted. The chat contrasted the conventional subpixel layouts of LCD office displays with the potential colored text-edge fringing of the QD-OLED G9. It also noted that a static editor interface is a consideration for OLED use.

## 🔌 Proposed Mac capture signal path

1. Connect **Mac Mini M1 HDMI output → UGREEN capture-card HDMI input**.
2. Connect **UGREEN HDMI loop-out → one monitor input** for the Mac PbP area.
3. Connect **Windows PC GPU → another monitor input** for the Windows PbP area.
4. Connect **UGREEN USB output → Windows PC** to make the Mac feed available to OBS.

The loop-out display signal and the USB feed into OBS have **separate format limits**. Early in the chat, the loop-out refresh rate was described as possibly 60Hz or 120Hz depending on the card. After the Amazon link identified ASIN `B0D4LV836Z`, a later answer asserted 2560×1440 at 120Hz through loop-out. The conversation did not establish the card’s specification or demonstrate that this mode works with the Mac and monitor. Likewise, a 1440p image on the monitor does not establish that OBS receives a native 1440p feed. Protected video may also be blocked in a capture-card signal chain.

A later Dell/LG connection suggestion instead put the Mac’s display on USB-C and sent a separate Mac video stream to the capture card. That is a different arrangement from recording the same HDMI signal used for the Mac’s PbP area; it was proposed but not worked through.

## 🎥 OBS and YouTube recording

A 2560×1440 Mac PbP area has the standard 16:9 shape used for YouTube video, unlike a recording of the entire 32:9 monitor. The chat suggested setting both OBS **Base (Canvas) Resolution** and **Output (Scaled) Resolution** to `2560x1440`, fitting the capture source to the canvas, and recording at 60 FPS with a hardware encoder and CQP 16–20.

Those settings define the *recorded file*, not the capture card’s input quality. If the USB feed is only 1080p, fitting it to a 1440p canvas enlarges 1080p source pixels; it does not restore missing code or text detail. The chat presented a 1440p upload as a way to obtain better YouTube compression, but its claim that this would **ensure** sharp text or a particular codec should not be treated as guaranteed. Verify the source format shown in OBS and inspect a short test upload before choosing final recording settings.

---

Source: <https://gemini.google.com/app/881d4eb7f61ae72b>
