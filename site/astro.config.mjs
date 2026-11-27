import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";

export default defineConfig({
  site: "https://seros-cimenstein.github.io",
  base: "/horus-drone",
  integrations: [
    starlight({
      title: "HORUS",
      description: "Safety-first, latency-tolerant autonomous drone supervision with ArduPilot and HORUS Studio.",
      logo: {
        src: "./src/assets/logo.svg",
      },
      favicon: "/favicon.svg",
      social: {
        github: "https://github.com/seros-cimenstein/horus-drone",
      },
      editLink: {
        baseUrl: "https://github.com/seros-cimenstein/horus-drone/edit/main/site/",
      },
      customCss: ["./src/styles/custom.css"],
      sidebar: [
        {
          label: "Getting Started",
          items: [
            { label: "Installation", slug: "getting-started/installation" },
            { label: "Quickstart", slug: "getting-started/quickstart" },
            { label: "AI Providers & Crew", slug: "getting-started/ai-providers" },
          ],
        },
        {
          label: "Core Concepts & Safety",
          items: [
            { label: "Architecture", slug: "concepts/architecture" },
            { label: "Safety Model", slug: "concepts/safety-model" },
            { label: "Vision & Latency", slug: "concepts/vision-and-latency" },
          ],
        },
        {
          label: "Simulation Worlds",
          items: [
            { label: "Built-in 3D World", slug: "simulation/builtin-3d" },
            { label: "Gazebo Harmonic", slug: "simulation/gazebo" },
            { label: "GTA V Integration", slug: "simulation/gta-v" },
          ],
        },
        {
          label: "Real Hardware & Field Ops",
          items: [
            { label: "Companion Setup", slug: "hardware/companion-setup" },
            { label: "Pre-flight & Testing", slug: "hardware/preflight-and-testing" },
            { label: "Phone Companion App", slug: "hardware/phone-app" },
          ],
        },
        {
          label: "Reference",
          items: [
            { label: "MCP Drone Board Tools", slug: "reference/mcp-tools" },
            { label: "CLI & Configuration", slug: "reference/cli-and-config" },
            { label: "Experiments & Logs", slug: "reference/experiments" },
            { label: "Credits & Licenses", slug: "reference/credits-and-licenses" },
          ],
        },
      ],
    }),
  ],
});
