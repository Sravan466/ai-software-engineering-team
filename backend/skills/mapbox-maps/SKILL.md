---
name: mapbox-maps
title: Maps with Mapbox
description: Use when the build shows maps with Mapbox.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [mapbox]
fixes: [mapbox, access token, mapbox-gl css]
---

The token is `NEXT_PUBLIC_MAPBOX_TOKEN`, a public token (`pk.`) — never a secret `sk.`
token, which can change the account. Restrict it by URL in the Mapbox account.

With `mapbox-gl`: set `mapboxgl.accessToken`, create the map in an effect on a container
with a fixed height, import `mapbox-gl/dist/mapbox-gl.css`, and call `map.remove()` when
the component unmounts. Render only on the client.

Add markers from your data; for many points use a GeoJSON source and a layer rather than
hundreds of DOM markers. Geocoding for stored addresses belongs on the server, cached.
