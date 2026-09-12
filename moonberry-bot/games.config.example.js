// games.config.example.js
// -----------------------------------------------------------------------
// TEMPLATE ONLY -- copy this file to games.config.js and fill in your
// real values there. games.config.js is gitignored and never committed;
// this example file is what's safe to keep in the public/shared repo.
// -----------------------------------------------------------------------

export const GAMES = {
  valheim: {
    displayName: "Valheim",
    emoji: "🌙",
    channelId: "REPLACE_WITH_YOUR_DISCORD_CHANNEL_ID",
    recommendedPassword: "REPLACE_WITH_YOUR_GROUP_PASSWORD",
    statusUrl: "https://your-valheim-coordinator.your-subdomain.workers.dev/status",
    statusSecret: "REPLACE_WITH_YOUR_COORDINATOR_SHARED_SECRET",
    statusBinding: "VALHEIM_COORDINATOR",
  },
  // Example of how a second game would be added later:
  // minecraft: {
  //   displayName: "Minecraft",
  //   emoji: "⛏️",
  //   channelId: "...",
  //   recommendedPassword: "...",
  //   statusUrl: "https://minecraft-sync-coordinator.example.workers.dev/status",
  //   statusSecret: "...",
  //   statusBinding: "MINECRAFT_COORDINATOR",
  // },
};
