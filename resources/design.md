// Version 1.1 - Color Palette Revision
// This .skill file defines the visual design tokens, component styles, and interaction patterns.
// Use this as the source of truth for all aesthetic and stylistic UI generation.

[01_META_AND_VIBE]
- **Design Paradigm:** Dark Mode, Glassmorphism, Gradient-Accented.
- **Emotional Resonance:** High-tech, clean, modern, immersive, fluid.
- **Density & Breathing Room:** "Airy cinematic." Generous use of negative space in navigation and hero sections, with content-dense grids for browsing.

[02_DESIGN_TOKENS_FOUNDATION]
//-- Color System (HEX & RGB) --//
//-- Palette updated based on user specification. --//
- **Backgrounds:**
    - `bg-primary`: "#000000" (Pure Black for immersive backgrounds)
    - `bg-surface`: "#1A1A1A" (Near Black for primary containers like settings panels)
    - `bg-surface-raised`: "#2C2C2E" (For interactive elements like input fields and dropdowns)
    - `bg-glass`: "rgba(26, 26, 26, 0.7)" (Semi-transparent for the top navigation bar with backdrop blur)
- **Foregrounds (Text):**
    - `text-primary`: "#F2F2F7" (Off White for main headings and active elements)
    - `text-secondary`: "rgba(242, 242, 247, 0.65)" (Approx 65% opacity of primary text for body copy, descriptions, and inactive nav links)
    - `text-muted`: "#94A3A8" (Muted Grey-Blue for tertiary info, placeholders, and disabled text)
- **Brand/Accent:**
    - `accent-primary`: "#8FE7EC" (Teal/Cyan, used for glows, focus rings, and primary interactive highlights)
    - `accent-secondary`: "#3D8F94" (Dark Teal, used as a secondary accent and in gradients)
    - `accent-gradient`: "linear-gradient(to right, #8FE7EC, #3D8F94)" (Teal/Cyan to Dark Teal, used in subtle background glows)
- **Semantic:**
    - `success`: "#34C759" (Green for success states)
    - `warning`: "#FFCC00" (Yellow for warnings)
    - `error`: "#FF3B30" (Red for errors)

//-- Typography Scale --//
- **Font Families:**
    - `font-primary`: "A modern, geometric Sans-Serif font like 'Circular Std', 'Inter', or 'system-ui'."
- **Weight Scale:**
    - `weight-body`: 400
    - `weight-medium`: 500 (For buttons, labels)
    - `weight-semibold`: 600 (For subheadings, active links)
    - `weight-bold`: 700 (For primary headings)
- **Size & Line-Height Hierarchy (Assume 1rem = 16px):**
    - `H1`: { size: "2.5rem", weight: 700, line-height: "1.2" } // "Discover", "What would you like to watch?"
    - `H2`: { size: "1.75rem", weight: 600, line-height: "1.3" } // Section titles like "Trending Movies This Week"
    - `H3`: { size: "1.25rem", weight: 600, line-height: "1.4" } // Settings section titles like "Playback"
    - `Body`: { size: "1rem", weight: 400, line-height: "1.5" } // Main descriptions, settings labels
    - `Small`: { size: "0.875rem", weight: 400, line-height: "1.4" } // Movie metadata (rating, year), footer text
    - `Caption`: { size: "0.75rem", weight: 500, line-height: "1.3" } // "View All" links

//-- Geometry & Spacing --//
- **Spacing Unit Base:** 8px system. All padding, margins, and gaps should be multiples of 8px.
    - `space-1`: "8px"
    - `space-2`: "16px"
    - `space-3`: "24px"
    - `space-4`: "32px"
    - `space-6`: "48px"
- **Border Radius:**
    - `radius-sm`: "4px" (For small tags/badges)
    - `radius-md`: "8px" (For buttons, inputs, interactive elements)
    - `radius-lg`: "12px" (Standard for movie posters and primary containers/cards)
- **Border Weights & Colors:**
    - `border-standard`: "1px solid #38383a" (Subtle border for inputs and containers)
    - `border-focus`: "2px solid #8FE7EC" (Focus ring for interactive elements, updated to new accent)

//-- Depth & Elevation (Shadows/Effects) --//
- **Shadows:** The system avoids traditional drop shadows. Depth is created with background colors and glows.
- **Glows & Blurs:**
    - `effect-glow`: A large, soft, blurred radial gradient of `accent-primary` placed behind elements in the background.
    - `effect-backdrop-blur`: "backdrop-filter: blur(12px);" Applied to the `bg-glass` navigation bar.

[03_COMPONENT_LIBRARY_SPECS]
- **Buttons:**
    - `button-primary`: { background: `accent-primary`, color: `bg-primary`, padding: "12px 24px", radius: `radius-md` } // NOTE: Text color is now dark for contrast.
    - `button-secondary`: { background: `bg-surface-raised`, color: `text-primary`, padding: "12px 24px", radius: `radius-md` }
    - `button-icon`: { background: "transparent", color: `text-secondary`, padding: "8px", radius: `radius-md` }
- **Cards (Movie Posters):**
    - `card-poster`: { aspect-ratio: "2 / 3", radius: `radius-lg`, overflow: "hidden" }
- **Forms & Inputs:**
    - `input-field`: { background: `bg-surface-raised`, color: `text-primary`, padding: "12px 16px", radius: `radius-md`, border: `border-standard` }
    - `input-placeholder`: { color: `text-muted` }
- **Toggle Switch:**
    - A standard iOS-style toggle. `bg-surface-raised` when off, `accent-primary` when on.
- **Navigation (Top Bar):**
    - `nav-bar`: { background: `bg-glass`, backdrop-filter: `effect-backdrop-blur`, position: "fixed", top: 0 }
    - `nav-link`: { color: `text-secondary`, weight: 500 }
    - `nav-link-active`: { color: `text-primary`, weight: 600 }

[04_COMPOSITION_AND_LAYOUT_RULES]
- **Container Max-Widths:** Main content should be constrained to a max-width of `1440px`, with generous horizontal padding (`space-6`) on the sides.
- **Alignment Paradigms:** Primarily left-aligned for section headers and content. Center-alignment is reserved for specific views like the main search page.

[05_INTERACTIVE_STATES_AND_MICRO_UI]
- **Hover States:**
    - Buttons & Links: Increase brightness/opacity slightly. `button-secondary` hovers to a lighter grey. Poster cards can have a subtle scale-up transform (`scale: 1.03`).
- **Focus States:**
    - Use a clear, visible focus ring: `outline: border-focus; outline-offset: 2px;`
- **Transitions:**
    - `transition-standard`: "all 200ms ease-in-out" for all interactive property changes (color, transform, background).

[06_LLM_EXECUTION_DIRECTIVES]
- **Framework Mapping:** Translate these tokens directly to CSS Custom Properties or a Tailwind CSS theme configuration.
- **Anti-Patterns:**
    - Do NOT use pure white (`#FFFFFF`) for large background areas.
    - Do NOT use harsh, sharp-edged drop shadows. Depth is created via color and blur.
    - Do NOT use default browser styling for any element.