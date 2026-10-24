import { createClient } from '@supabase/supabase-js';
import crypto from 'crypto';

const rawSupabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || 'https://placeholder.supabase.co';
const supabaseUrl = rawSupabaseUrl.trim().replace(/\/+$/, '').replace(/\/rest\/v1\/?$/, '');
const supabaseAnonKey = (process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || 'placeholder-anon').trim();
const supabaseServiceRoleKey = process.env.SUPABASE_SERVICE_ROLE_KEY ? process.env.SUPABASE_SERVICE_ROLE_KEY.trim() : undefined;

// Cliente público para el frontend y consultas autorizadas con RLS
export const supabase = createClient(supabaseUrl, supabaseAnonKey);

// Cliente administrativo (solo servidor) para verificar API keys y operaciones críticas
export const supabaseAdmin = createClient(
  supabaseUrl,
  supabaseServiceRoleKey || supabaseAnonKey,
  {
    auth: {
      autoRefreshToken: false,
      persistSession: false
    }
  }
);

/**
 * Genera el hash SHA-256 de una API key para almacenamiento y comparación segura
 */
export function hashApiKey(key: string): string {
  return crypto.createHash('sha256').update(key.trim()).digest('hex');
}

/**
 * Valida si las credenciales de Supabase están configuradas realmente o son de placeholder
 */
export function isSupabaseConfigured(): boolean {
  return Boolean(
    process.env.NEXT_PUBLIC_SUPABASE_URL &&
    !process.env.NEXT_PUBLIC_SUPABASE_URL.includes('placeholder') &&
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY &&
    !process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY.includes('placeholder')
  );
}
