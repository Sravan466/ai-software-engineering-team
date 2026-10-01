---
name: s3-storage
title: File storage on AWS S3
description: Use when the build stores files in an S3 bucket.
agents: [backend_engineer, frontend_engineer, security_engineer]
keywords: [s3, amazon s3, aws s3]
fixes: [s3, presigned url, access key, bucket policy]
---

`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are read on the server only, with
`AWS_REGION` and `S3_BUCKET`. The SDK reads the keys and region by those names.

Uploads go straight from the browser to S3 with a presigned URL: your API route checks
who is signed in, picks the object key itself (never the browser's filename as a path),
and returns `getSignedUrl(s3, new PutObjectCommand({ Bucket, Key, ContentType }), {
expiresIn: 300 })`. Downloads of private files work the same way with `GetObjectCommand`.

Keep the bucket private — no public ACLs — and store object keys, not URLs, in your
data. The IAM user should have access to this one bucket only.
