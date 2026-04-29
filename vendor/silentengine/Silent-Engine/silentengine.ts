// File: silentengine.ts
// Pulled from rsemeah/Silent-Engine main
// Source: https://github.com/rsemeah/Silent-Engine/blob/main/silentengine.ts

import fs from 'node:fs';
import path from 'node:path';
import cron from 'node-cron';
import { fileURLToPath } from 'node:url';
import dotenv from 'dotenv';

import { Logger } from './src/logger.js';
import { Router } from './src/router.js';
import { Engine } from './src/engine.js';
import { RateLimiter } from './src/ratelimiter.js';
import { DashboardDataService } from './src/dashboard-data.js';
import { MockProvider } from './src/providers/mock.js';
import { OpenAIProvider } from './src/providers/openai.js';
import { AnthropicProvider } from './src/providers/anthropic.js';
import { GeminiProvider } from './src/providers/gemini.js';
import { GroqProvider } from './src/providers/groq.js';
import { MistralProvider } from './src/providers/mistral.js';
import { IProvider } from './src/providers/base.js';
import { createServer } from './src/app/server.js';

const envFile = path.resolve(process.cwd(), '.env.silentengine');
if (fs.existsSync(envFile)) {
  dotenv.config({ path: envFile });
} else {
  dotenv.config();
  console.warn('⚠️  .env.silentengine not found. Falling back to default environment.');
}

process.env.NODE_ENV =
  process.env.SILENTENGINE_ENV || process.env.NODE_ENV || 'development';

validateProductionSecurity();

const logger = new Logger('./logs');
const router = new Router();
const providers = new Map<string, IProvider>();

const enabledProviders = new Set(
  (process.env.SILENTENGINE_ENABLED_PROVIDERS || '')
    .split(',')
    .map((provider) => provider.trim())
    .filter(Boolean)
);

function isProviderEnabled(name: string) {
  return !enabledProviders.size || enabledProviders.has(name);
}

function registerProvider(model: string, provider: IProvider) {
  providers.set(model, provider);
  console.log(`✅ Registered ${model} -> ${provider.getName()}`);
}

function registerMock(model: string) {
  console.warn(`⚠️  No API key configured for ${model}. Using mock provider.`);
  registerProvider(model, new MockProvider(model));
}

if (isProviderEnabled('groq')) {
  const groqKey = process.env.GROQ_API_KEY;
  if (groqKey) {
    registerProvider(
      'groq-llama-3.1-70b',
      new GroqProvider('groq-llama-3.1-70b', groqKey, {
        apiModel: 'llama-3.1-70b-versatile',
      })
    );
  } else {
    registerMock('groq-llama-3.1-70b');
  }
}

if (isProviderEnabled('claude')) {
  const anthropicKey = process.env.ANTHROPIC_API_KEY;
  if (anthropicKey) {
    registerProvider(
      'claude-haiku-4',
      new AnthropicProvider('claude-haiku-4', anthropicKey, {
        apiModel: 'claude-3-haiku-20240307',
      })
    );
  } else {
    registerMock('claude-haiku-4');
  }
}

if (isProviderEnabled('gemini')) {
  const googleKey = process.env.GOOGLE_API_KEY;
  if (googleKey) {
    registerProvider('gemini-1.5-flash', new GeminiProvider('gemini-1.5-flash', googleKey));
  } else {
    registerMock('gemini-1.5-flash');
  }
}

if (isProviderEnabled('openai')) {
  const openaiKey = process.env.OPENAI_API_KEY;
  if (openaiKey) {
    registerProvider('gpt-4o-mini', new OpenAIProvider('gpt-4o-mini', openaiKey));
  } else {
    registerMock('gpt-4o-mini');
  }
}

if (isProviderEnabled('mistral')) {
  const mistralKey = process.env.MISTRAL_API_KEY;
  if (mistralKey) {
    registerProvider('mistral-large-latest', new MistralProvider('mistral-large-latest', mistralKey));
  }
}

const engine = new Engine(providers, router, logger);
const rateLimiter = new RateLimiter();
rateLimiter.setLimit('hirewire-api-key', {
  windowMs: 60 * 1000,
  maxRequests: 200,
  maxCost: 5,
});

const dashboardData = new DashboardDataService(logger);
const app = createServer({ engine, dashboardData, rateLimiter });

cron.schedule('0 2 * * *', async () => {
  console.log('🧹 Running log archive job...');
  await logger.archiveOldLogs(30);
});

const port = Number(process.env.SILENTENGINE_PORT || process.env.PORT || 5050);
let server: ReturnType<typeof app.listen> | null = null;

if (isDirectRun()) {
  server = app.listen(port, () => {
    console.log(`🧠 SilentEngine listening on port ${port} (env=${process.env.NODE_ENV})`);
  });

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

function shutdown() {
  if (!server) {
    return;
  }
  console.log('👋 Shutting down SilentEngine runtime...');
  logger.shutdown();
  server.close(() => process.exit(0));
}

function isDirectRun(): boolean {
  if (typeof process === 'undefined' || !process.argv?.[1]) {
    return false;
  }
  const currentFile = fileURLToPath(import.meta.url);
  return path.resolve(process.argv[1]) === currentFile;
}

function validateProductionSecurity() {
  if (process.env.NODE_ENV === 'production') {
    if (
      !process.env.ENGINE_API_KEY ||
      process.env.ENGINE_API_KEY === 'dev-key-change-in-production'
    ) {
      throw new Error('❌ FATAL: ENGINE_API_KEY must be set to a secure value in production');
    }
    if (process.env.ENGINE_API_KEY.length < 32) {
      throw new Error('❌ FATAL: ENGINE_API_KEY must be at least 32 characters');
    }
    const providerKeys = [
      process.env.GROQ_API_KEY,
      process.env.ANTHROPIC_API_KEY,
      process.env.GOOGLE_API_KEY,
      process.env.OPENAI_API_KEY,
      process.env.MISTRAL_API_KEY,
    ].filter(Boolean);
    if (!providerKeys.length) {
      throw new Error('❌ FATAL: At least one provider API key must be configured');
    }
    console.log('✅ Production security checks passed');
  }
}

export default app;
export { app, engine, rateLimiter, dashboardData };
