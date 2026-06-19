// The @types package doesn't declare the per-file theme imports under styles/prism/*.
// Declaring them here (as untyped modules) keeps `tsc` happy when importing a single theme
// file directly (which avoids pulling the whole styles index into the bundle).
declare module "react-syntax-highlighter/dist/esm/styles/prism/*";
