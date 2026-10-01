---
name: cloudinary-media
title: Images and video with Cloudinary
description: Use when the build uploads, transforms or serves images and video through Cloudinary.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [cloudinary]
fixes: [cloudinary, signed upload, api secret, upload preset]
---

`CLOUDINARY_API_KEY` and `CLOUDINARY_API_SECRET` are read on the server only.
`NEXT_PUBLIC_CLOUDINARY_CLOUD_NAME` is public and is all the browser needs to show images.

Uploads from the browser are signed: the page asks your API route for a signature
(`cloudinary.utils.api_sign_request({ timestamp, folder }, API_SECRET)`), then uploads
straight to Cloudinary with that signature, the timestamp and the API key. The secret
never reaches the page. Restrict folder, size and format on the server side of that
signature.

Store the returned `public_id`, not the full URL, and build delivery URLs with
transformations (`f_auto,q_auto`, a width) so pages get small images. Delete with the
admin API from the server when the owning record is deleted.
