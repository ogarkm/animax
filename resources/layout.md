// UI Layout & Composition System
// Version 1.0
// This .skill file defines the structure, composition, and arrangement of UI components on different pages.
// Use this file to understand the "skeleton" and spatial relationships of the application.

[01_GLOBAL_STRUCTURE]
- **Header (`<header>`):**
    - A fixed (sticky) top navigation bar that spans the full width of the viewport.
    - Contains the site logo, primary navigation links, a search icon, and user profile/notification icons.
    - Height should be approximately `72px`.
- **Main Content (`<main>`):**
    - The primary container for all page-specific content.
    - Sits below the header.
    - Uses a consistent horizontal padding on the left and right (`space-6` or 48px).
- **Footer (`<footer>`):**
    - A simple, minimal footer.
    - Contains the site logo again, a brief description, and secondary links (e.g., "Settings").
    - Has significant top margin to separate it from the last content block (`~100px`).

[02_PAGE_SPECIFIC_LAYOUTS]

//-- Layout: Homepage --//
- **Component Order:**
    1. **Hero Section:**
        - Full-bleed section at the top, often with a large, atmospheric background image from a featured title.
        - Contains the title's logo (as a large graphic), metadata (rating, year, duration), a short synopsis, and primary call-to-action buttons ("Play", "Trailer").
        - Content is left-aligned and vertically centered within the section.
    2. **Content Carousels (Rows):**
        - A series of horizontally-scrolling rows.
        - Each row has a `H2` Section Title (e.g., "Trending Movies This Week") and a "View All" link aligned to the right.
        - The title has a bottom margin of `space-3` (24px) before the grid of posters.
        - The posters within the carousel form a grid with a `gap` of `space-3` (24px).
        - Multiple carousels are stacked vertically with a large `gap` between them (`space-6` or 48px).

//-- Layout: Discover Page --//
- **Component Order:**
    1. **Page Header:**
        - A main `H1` title: "Discover".
    2. **Filter Bar:**
        - A horizontal container below the header with several dropdown components for filtering (e.g., "All Genres", "All Providers", "Most Popular", "From Year").
        - A "Surprise Me" button is aligned to the far right of this bar.
        - The filter bar has a bottom margin of `space-6` (48px).
    3. **Content Grid:**
        - A responsive grid of movie/series poster cards.
        - The grid should display 5-6 columns on a wide desktop view.
        - The `gap` between grid items should be `space-3` (24px).
    4. **Pagination:**
        - Centered below the grid.
        - Contains numbered page links and "Previous"/"Next" buttons.

//-- Layout: Settings Page --//
- **Component Order:**
    1. **Page Header:**
        - A main `H1` title: "Settings".
    2. **Main Settings Container:**
        - This is the primary layout element, a single, large card-like container with a `bg-surface` color.
        - It is divided into two columns with a vertical divider.
        - **Left Column (Navigation):** A vertical list of navigation links for different settings categories (e.g., "Playback", "Home Layout", "Subtitles"). The active category is highlighted.
        - **Right Column (Content):** Displays the settings for the currently selected category.
            - Content is organized into sub-sections, each with a clear `H3` title (e.g., "Appearance", "Home Layout").
            - Each setting item is a row containing a label on the left and a control (toggle switch, dropdown) on the right. Rows are separated by dividers.

//-- Layout: Search Page --//
- **Component Order:**
    1. **Vertically & Horizontally Centered Content:**
        - All content is centered on the page.
    2. **Main Heading (`H1`):** "What would you like to watch?"
    3. **Search Type Selector:** Two small, connected buttons to switch between "Title Search" and "AI Search".
    4. **Search Input Field:** A large, wide search bar with a search icon inside. It should be the primary focus of the page.

//-- Layout: Details Page --//
- **Component Order:**
    1. **Main Details Section:**
        - This section is often overlaid on a blurred background of the movie poster.
        - A large poster image is on the left.
        - To the right of the poster is the title, metadata tags (year, rating, genre), action buttons ("Play", "Trailer", "Add to List"), and a detailed synopsis.
    2. **Cast & Crew Section:**
        - A horizontally scrolling carousel below the main details.
        - Header: `H2` "Cast".
        - Items: Circular avatars of actors with their names below.
    3. **Related Content Section:**
        - Another content carousel, similar to the homepage, with a title like "You Might Also Like".

[03_LLM_EXECUTION_DIRECTIVES]
- **Semantic HTML:** Use semantic tags (`<nav>`, `<main>`, `<section>`, `<footer>`, `<header>`) to structure the generated code according to these layouts.
- **Grid & Flexbox:** Use CSS Flexbox for carousels and simple alignments (like filter bars). Use CSS Grid for the main content grids (e.g., the Discover page).
- **Responsiveness:** While the images are desktop-focused, infer responsive behavior. The multi-column grids should collapse to fewer columns on smaller viewports. The settings page may stack its two columns on mobile.