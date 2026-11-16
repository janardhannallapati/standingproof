import type { NextConfig } from "next";

const config: NextConfig = {
  // A self-contained server in .next/standalone, so the image needs no node_modules.
  output: "standalone",
  // Say nothing about the framework in response headers.
  poweredByHeader: false,
  reactStrictMode: true,
};

export default config;
