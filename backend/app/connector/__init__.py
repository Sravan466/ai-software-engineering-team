"""The connector: a user's own computer, paired to an account, serving its models.

`protocol` is shared by both ends — this server and the `aiteam_connect` program on
the user's computer — so what one sends is what the other validates. `pairing` and
`hub` are the server's half.
"""
