---
name: accessible-ui
title: Accessible interfaces
description: Use when building any user interface — semantics, keyboard operation, focus, contrast, and messages assistive technology can reach.
agents: [frontend_engineer, qa_engineer]
keywords: [accessibility, a11y, aria, keyboard, screen reader, contrast, focus, wcag, semantic, ui, form, frontend, page, component]
---

Use the element that already means the thing. A `button` is focusable, activates on
Enter and Space, and announces itself; a `div` with a click handler does none of that
and needs four attributes to pretend. Reach for ARIA only when no element fits.

Everything operable by mouse is operable by keyboard, in an order that matches what is
on screen. Tab reaches every control, Enter and Space activate, Escape closes what
opened. Anything that traps focus without a way out is broken for keyboard users.

Focus is visible at all times. Removing the outline without putting a stronger
indicator in its place leaves a keyboard user with no idea where they are.

Every input has a real label tied to it — a placeholder disappears the moment someone
types and was never announced as a name. Group related controls in a fieldset with a
legend, and mark required fields in text as well as colour.

Errors say what is wrong and how to fix it, sit next to the field they belong to, and
are tied to it so a screen reader announces them. Put a live region on anything that
changes without a page load, so "Saved" and "Could not save" are not silent.

Keep text contrast at 4.5:1, and 3:1 for large text and for the visual edges of
controls. Never let colour be the only carrier of meaning — a red border needs an icon
or words beside it.

Give every image a text alternative, and an empty one when the image is decoration.
Name icon-only buttons in text a screen reader can read.

Respect the reader: a single logical heading order, no positive tabindex values, and
motion reduced when the system asks for it.
