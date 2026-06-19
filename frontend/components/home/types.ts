// Tabs on the merged home page. "home" = the Blueprint landing board,
// "chats" = previous chats + new-chat creation. Kept in its own module so
// both the client experience and the (server) page can share the literal.
export type HomeTab = "home" | "chats";
