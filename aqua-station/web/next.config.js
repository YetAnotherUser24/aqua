/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static export: the build emits plain HTML/CSS/JS into out/, which Caddy serves
  // straight off the SD card. There is no Node process on the Pi at all, which is
  // worth roughly 150-250 MB on a 905 MB machine.
  //
  // The consequence to remember: no server-side code may exist in this app. Anything
  // that needs to run per-request lives in gateway/ and is reached over /api/*, which
  // Caddy proxies on the same origin so relative URLs keep working unchanged.
  output: 'export',

  reactStrictMode: true,

  // No image optimizer exists without a server.
  images: { unoptimized: true },

  // Type errors must fail the build. A dashboard that renders a stale field name as
  // `undefined` is worse than one that refuses to compile, because it looks plausible.
  typescript: { ignoreBuildErrors: false },

  // ESLint is not part of this scaffold; `npm run typecheck` is the gate that matters.
  eslint: { ignoreDuringBuilds: true },
};

module.exports = nextConfig;
