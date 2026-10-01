---
name: google-maps
title: Maps with Google Maps
description: Use when the build shows maps, places or directions with Google Maps.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [google maps, google map]
fixes: [google maps, api key, referrer restriction, map id]
---

The key is `NEXT_PUBLIC_GOOGLE_MAPS_API_KEY`: public by design, because the Maps
JavaScript API runs in the browser. Its protection is a referrer restriction set in
Google Cloud, not secrecy — say so in the README.

In React use `@vis.gl/react-google-maps`: one `<APIProvider apiKey={…}>` near the top,
then `<Map>` with `<AdvancedMarker>`s. Render maps only on the client.

Geocoding addresses for storage belongs on the server, where it can be cached — don't
geocode the same address on every page view. Keep coordinates in your data as numbers,
and give the map a fixed height so it doesn't collapse to zero.
