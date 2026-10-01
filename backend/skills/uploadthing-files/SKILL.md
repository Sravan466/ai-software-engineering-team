---
name: uploadthing-files
title: File uploads with UploadThing
description: Use when the build lets people upload files through UploadThing.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [uploadthing, upload thing]
fixes: [uploadthing, file router, uploadthing_token]
---

`UPLOADTHING_TOKEN` is read on the server only, by the SDK, from that exact name.

Define one file router (`createUploadthing()`) with a route per kind of upload, each with
its own limits: `image: { maxFileSize: "4MB", maxFileCount: 1 }`. In each route's
`.middleware`, check who is signed in and throw when nobody is — an open upload route is
free storage for anyone. Return the user id from middleware so `onUploadComplete` can
record who uploaded what.

Mount the route handler at `app/api/uploadthing/route.ts` and use the generated
`UploadButton` / `UploadDropzone` in the page. Save the file key and URL in your own data
in `onUploadComplete`.
