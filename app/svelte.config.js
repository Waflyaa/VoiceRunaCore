import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/vite-plugin-svelte').SvelteConfig} */
export default {
  preprocess: vitePreprocess(),
  compilerOptions: {
    // The whole UI is written with Svelte 5 runes; legacy syntax is a bug here.
    runes: true,
  },
};
