---
date: 2026-05-08
type: codex-handoff
target_model: GPT Image 2 (OpenAI image generation; gpt-image-1 / DALL-E 3 successor)
purpose: LinkedIn / social media infographic
status: ready-for-codex
deliverable: 1080×1350 PNG (4:5 portrait); 3-5 variant generations
operator: keith
---

# Codex handoff: K2Bi coach-pipeline infographic for LinkedIn

## Goal

Generate a LinkedIn-ready infographic that visually communicates the **K2Bi coach pipeline** (T1 → T11 → T12) -- the disciplined process by which AI helps build a trading thesis under hard human-judgment gates. Audience: senior operators on LinkedIn / social media demonstrating "AI + rigor" rather than "AI hype."

The infographic accompanies a LinkedIn post by Keith demonstrating his real Phase 3.8b first paper trade (Genpact NYSE: G) -- the entire pipeline ran end-to-end through 11 disciplined gates and produced a single small position with bounded downside.

## What the K2Bi pipeline is (context for Codex)

K2Bi is Keith's AI second-brain for trading thesis building. The "coach" skill walks Keith through 11 disciplined turns from raw narrative intuition to operator-approvable strategy spec. Each turn has a specific purpose; some turns are **discipline gates** that AI alone cannot pass -- they require primary-source verification, adversarial bear-case testing, sanity-check backtests, and management-guidance comparison.

The narrative for LinkedIn: *"AI helped me build my first trading thesis. But AI alone wasn't enough. Here are the 11 disciplined gates that made it work."*

### Pipeline stages (grouped into 4 visual phases for the infographic)

**Phase 1 -- DISCOVERY (T1-T3): "From narrative intuition to single ticker"**
- T1: Lived signal (operator's narrative intuition + domain knowledge)
- T2: Refined narrative (structured framing of hypothesis)
- T3: Cross-vendor AI discovery (Kimi DR + GPT DR independent search converge on candidate ticker)

**Phase 2 -- ANALYSIS (T4-T6): "From ticker to verified thesis"**
- T4: Manual screen (Quick Score + ARK 6-metric scoring)
- T5: Source verification (every URL HEAD-verified at primary source)
- T6: Ahern 4-phase thesis (Business Model / Competitive Moat / Financial Quality / Risks + Valuation)
- T6 sub-scores: Bull/Base/Bear asymmetry analysis (EV-weighted scenarios)

**Phase 3 -- DISCIPLINE GATES (T7, T8, T9, T11): "Where AI must prove itself"** ← VISUAL CENTERPIECE
- T7: Verification gate (every load-bearing claim verified at primary-source level by operator)
- T8: Bear-case adversary (single adversarial AI call returns VETO or PROCEED)
- T9: Backtest sanity check (yfinance 2-year window; look-ahead bias check)
- T11: Forward-guidance check (latest management guidance vs strategy thresholds)

**Phase 4 -- APPROVAL (T10, T12): "From verified thesis to live trade"**
- T10: Strategy spec drafting (concrete share count + 4 exit triggers + mandatory plain-English section)
- T12: Operator approval handoff (`/invest-ship --approve-strategy` fires the trade at next open)

## Output specs

- **Format:** PNG, 1080×1350 pixels (4:5 portrait; LinkedIn mobile-feed optimal)
- **Color depth:** sRGB
- **Generations:** 3-5 variations using the primary prompt; if first batch is weak, try variant prompts B and C
- **Save path:** `~/Projects/K2Bi-Vault/Assets/images/2026-05-08_coach-pipeline-infographic_v{N}.png`

## Primary prompt (use this first)

```
A premium vertical infographic for a LinkedIn post about how AI helps build trading theses with hard discipline gates. The mood is "AI + rigor," not "AI hype" -- think Bloomberg meets Apple, not crypto-bro neon.

Format: 4:5 portrait orientation (1080×1350), optimized for mobile LinkedIn feed.

Aesthetic: deep navy background (#0A1628) with subtle gradient. Gold accents (#D4AF37) for connector lines and highlights. Clean white sans-serif text (think Inter or SF Pro). Minimalist line-art icons -- no skeuomorphic detail. Subtle data-mesh grid in the far background, very faint.

Structure: vertical 4-phase flow, top to bottom, with each phase visually distinct via color tone but unified through gold connector lines linking sub-stages.

HEADER (top of image, ~10% of vertical space):
Bold title: "Building a Trading Thesis With AI"
Subtitle one line below: "11 disciplined gates from intuition to approval"
Small AI co-pilot icon in upper-right corner (subtle, glowing)

PHASE 1 — DISCOVERY (warm gold tone, ~15% of image, top section):
Section caption in gold: "DISCOVERY — From narrative intuition to single ticker"
Three small icon nodes connected horizontally with gold dotted lines:
- Lightbulb icon, label "Lived signal"
- Two-overlapping-circles icon, label "Cross-vendor AI search"
- Single map-pin icon, label "1 candidate ticker"

PHASE 2 — ANALYSIS (cool blue tone, ~20% of image, second from top):
Section caption in cool blue: "ANALYSIS — From ticker to verified thesis"
Four icon nodes:
- Matrix-grid icon, label "Quantitative screen"
- Checkmark-shield icon, label "Source verification"
- Four-quadrant icon, label "4-phase thesis"
- Three-bar scale icon, label "Bull / Base / Bear asymmetry"

PHASE 3 — DISCIPLINE GATES (bold red-orange tone, ~35% of image, MIDDLE — VISUAL CENTERPIECE):
Section caption in bold red-orange, slightly larger font: "DISCIPLINE GATES — Where AI must prove itself"
Four checkpoint-barrier icons in series, each slightly larger than other phase elements, rendered as glowing red-orange gates the data must pass through. The gates feel substantive — like security checkpoints, not decorative flourishes:
- Gate 1: shield with primary-source-document icon. Label: "Verification gate"
- Gate 2: shield with crossed-swords icon. Label: "Bear-case adversary"
- Gate 3: line-chart-with-magnifying-glass icon. Label: "Backtest sanity"
- Gate 4: megaphone-with-checkmark icon. Label: "Forward-guidance check"

This phase should occupy the largest vertical real estate. The four gates are the visual heart of the image. Use thicker connector lines, slight glow effect on the gate icons, and ensure the red-orange palette draws the eye.

PHASE 4 — APPROVAL (emerald green tone, ~15% of image, bottom):
Section caption in emerald: "APPROVAL — From verified thesis to live trade"
Two icon nodes:
- Blueprint icon, label "Strategy spec drafted"
- Green-checkmark icon, label "Operator approval — trade fires"

FOOTER (bottom of image, ~5% of vertical space):
Single horizontal strip with 4 key outcome stats in white text on gold-tinted band. Render numbers larger and clearer than other text:
- "24 claims verified"
- "Bear-case 55% conviction (passed)"
- "0.25% NAV at-risk"
- "Net EV +14.4%"

Visual hooks:
- Gold dotted connector lines weave through all phases, creating a single continuous "data flow" from top to bottom
- The discipline-gates phase is the visual hero: largest, bold red-orange, glowing
- AI co-pilot icon in header corner suggests "AI assists throughout"
- Subtle network/data-mesh grid in far background (very faint, ~5% opacity)
- No charts. No stock-photo trading floors. No cartoon brokers.

Tone: Disciplined. Premium. Senior-operator audience. Conveys "AI as instrument under human-judgment discipline" not "AI as magic" or "AI as autonomous trader."

Negative prompts (avoid): stock photo aesthetics, charts with arrows going up dramatically, cartoonish icons, neon/crypto-bro colors, cluttered text, generic AI-brain illustrations, robot avatars, dollar-sign clichés.
```

## Variant prompt B — abstract waterfall (try if primary feels too rigid)

```
A vertical infographic depicting trading-thesis construction as a continuous waterfall of data flowing through filter membranes. 4:5 portrait, 1080×1350 pixels, mobile-first.

Aesthetic: Bloomberg-Apple premium fintech. Deep navy (#0A1628) background with subtle gradient. Gold (#D4AF37) flow lines representing data movement. White sans-serif type.

Top of image: a "narrative spark" — abstract visualization of an operator's lived signal, like a glowing neuron or constellation pattern, in warm gold. Title above: "Building a Trading Thesis With AI."

Below the spark, the data flows downward through FOUR HORIZONTAL MEMBRANES (filter layers), each a distinct color band:
1. Gold band: "Discovery" — the spark gets refined into structured narrative, then cross-vendor AI search converges on a single ticker
2. Cool blue band: "Analysis" — the ticker gets quantitative screen + source-verified + 4-phase thesis + asymmetry weights
3. Red-orange band (LARGEST and most prominent): "Discipline gates" — the data must pass through 4 narrowing checkpoints visualized as glowing barriers (verification / bear-case / backtest / forward-guidance)
4. Emerald green band: "Approval" — the data emerges as a single concrete trade

Each band has its name + a one-line caption. Sub-stages within each band are subtle icon callouts on the side, not blocking the main flow.

The red-orange "discipline gates" band is the visual centerpiece. Make the 4 gates feel like real obstacles — narrow openings, glowing edges, the data has to squeeze through.

Footer: thin gold strip with 4 outcome stats, larger text. Same as primary prompt footer.

Tone: continuous, flowing, disciplined. The waterfall metaphor is "raw signal → refined trade through layered filters." AI is the medium of the flow; discipline is the filter.

Avoid stock-photo waterfalls or actual water imagery. The flow is data/light, not liquid.
```

## Variant prompt C — engineering blueprint (try if primary feels too storytelling-heavy)

```
A technical engineering blueprint of an 11-stage trading-thesis pipeline, rendered in classic blueprint aesthetic. 4:5 portrait, 1080×1350 pixels.

Style: navy-blue background (#0A2540) with white/cream line work, like a real architectural drawing. Title block in bottom-right corner with project metadata. Grid paper texture subtle in background. Drafting-style technical labels with leader lines.

Layout: pipeline runs left-to-right horizontally OR top-to-bottom vertically (your choice based on what reads best in 4:5 portrait). 11 numbered components labeled T1 through T11 + final T12 approval node.

Each component is rendered as a precise technical icon:
- T1: signal-source symbol
- T2: filter/funnel symbol
- T3: parallel-vendor splitter symbol (two paths converging)
- T4: scoring matrix
- T5: validation-checkmark
- T6: 4-quadrant matrix
- T6.5: probability-distribution scale
- T7: gate / checkpoint with verification stamp
- T8: adversarial-test chamber
- T9: time-series chart with sanity-check border
- T10: spec-document with parameters
- T11: comparison-gate with pass/fail
- T12: green approval valve

Connect all components with precise drafting-style lines: solid for primary flow, dashed for feedback loops, dotted for advisory paths.

In the title block (bottom-right corner): "K2BI COACH PIPELINE / Phase 3.8b First Paper Trade / Drawing No. T1-T12 / Scale: NTS / Date: 2026-05-08"

Side annotations: small callouts naming each phase and listing key constraints (e.g. "T7: every claim primary-source verified," "T8: 70% VETO threshold," "T11: management guide vs strategy thresholds").

Aesthetic reference: think NASA technical drawings, Boeing engineering schematics, or a Patek Philippe movement diagram. Precise, beautiful, severe. Not playful. Not modern-flat-design. Old-school technical drafting brought into a premium fintech context.

No icons should look cute or cartoonish. Every line is functional. The viewer should feel "this is a serious system."
```

## Acceptance criteria

A generation is acceptable if:

1. **Visual hierarchy clear:** discipline gates (Phase 3) are obviously the most prominent section
2. **4-phase grouping legible:** viewer can identify the 4 phases without reading every label
3. **LinkedIn-mobile-readable:** key elements visible on a phone screen scrolling at normal speed
4. **AI + rigor messaging:** image conveys "AI under human discipline," not "AI does it all"
5. **No glaring rendering issues:** numbers in footer are clearly readable; no fake-language text artifacts
6. **Premium aesthetic:** does not look like a free Canva template

If first 3-5 generations from primary prompt fail criteria 1-3, escalate to variant B (waterfall). If still failing, try variant C (blueprint) which is more text-tolerant via drafting-style annotations.

## Known model limitations to expect

- **Inline text often garbled.** Image models render some text accurately and some as fake-language squiggles. The footer outcome stats are most likely to render cleanly because they're large and isolated. Stage labels inside icons may need post-processing if you want exact text fidelity.
- **Icon consistency varies.** GPT Image 2 may render icons in slightly inconsistent styles across the same image. If consistency matters, generate the structure without text labels first, then add text overlay in a graphics editor.
- **Color palette adherence:** the model usually respects color hex codes but may drift toward its training-distribution defaults. Confirm hex codes hold across the 3-5 generation batch before picking the winner.

## Iteration guidance

- If the first 5 generations from primary prompt look "fine but boring," try variant B for more visual energy.
- If they look "cluttered," try variant C (blueprint) which forces sparser composition.
- If specific stages render poorly, simplify by reducing labels and let icons carry meaning.
- If text in footer corrupts, generate without footer and add stats overlay in post-processing using SF Pro Display Bold at 24pt over a gold-tinted strip.

## Deliverable

Place the chosen image (and ideally 1-2 alternates) at:
`~/Projects/K2Bi-Vault/Assets/images/2026-05-08_coach-pipeline-infographic_v{N}.png`

Notify operator when ready. Operator will pair with the LinkedIn post copy in a separate session.

## Out-of-scope for this handoff

- Writing the LinkedIn post copy (Keith handles separately)
- Animated/video versions (this is single-image only)
- Multiple ratios (4:5 portrait only; no 1:1 or 1.91:1 alternates needed)
- Editing the K2Bi vault content (this handoff only generates an asset; vault writes are coach's domain)
