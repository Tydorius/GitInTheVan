<script lang="ts">
  import { onMount } from 'svelte'
  import { api } from '../../api'

  let sslStatus: any = $state(null)
  let sslIPs = $state('')
  let sslGenerating = $state(false)
  let error = $state('')

  async function loadSSL() {
    try {
      sslStatus = await api.getSSLStatus()
    } catch { sslStatus = null }
  }

  async function generateCert() {
    error = ''
    sslGenerating = true
    try {
      const ips = sslIPs.trim() ? sslIPs.trim().split(',').map(s => s.trim()).filter(Boolean) : undefined
      sslStatus = await api.generateSSLCert(ips)
    } catch (e: any) { error = e.message }
    finally { sslGenerating = false }
  }

  onMount(loadSSL)
</script>

{#if error}<div class="error-msg">{error}</div>{/if}

<div class="card">
  <h3>HTTPS / LAN Access</h3>
  <p style="color: var(--text-dim); font-size: 12px; margin-bottom: 16px;">
    Browsers block HTTP requests from HTTPS sites (like JanitorAI). Enabling HTTPS allows
    other devices on your network to use GitInTheVan as a reverse proxy.
    This uses a self-signed certificate &mdash; you must accept it once in each browser.
  </p>

  {#if sslStatus?.is_active}
    <div class="success-msg" style="margin-bottom: 12px;">
      HTTPS is active. Access from other devices via <code>https://YOUR-IP:{location.port}</code>
    </div>
  {:else if sslStatus?.cert_exists}
    <div style="background: var(--bg-elevated); padding: 12px; border-radius: 8px; margin-bottom: 12px; font-size: 12px;">
      Certificate generated but not yet active. Restart the server to enable HTTPS.
    </div>
  {:else}
    <div style="background: var(--bg-elevated); padding: 12px; border-radius: 8px; margin-bottom: 12px; font-size: 12px;">
      No certificate configured. The server is running over HTTP.
    </div>
  {/if}

  {#if sslStatus?.cert_info}
    <div style="font-size: 11px; color: var(--text-dim); margin-bottom: 16px;">
      <div><strong>Subject:</strong> {sslStatus.cert_info.subject}</div>
      <div><strong>Valid until:</strong> {new Date(sslStatus.cert_info.not_after).toLocaleDateString()}</div>
    </div>
  {/if}

  <div class="form-group">
    <label for="ssl-ips">Include IP addresses in certificate (comma-separated, optional)</label>
    <input id="ssl-ips" bind:value={sslIPs} placeholder="10.0.0.187, 192.168.1.50" />
    <p style="font-size: 11px; color: var(--text-dim); margin-top: 4px;">
      Add your machine's LAN IP(s) so browsers trust the cert for those addresses.
    </p>
  </div>

  <button class="primary" onclick={generateCert} disabled={sslGenerating}>
    {sslGenerating ? 'Generating...' : sslStatus?.cert_exists ? 'Regenerate Certificate' : 'Generate Self-Signed Certificate'}
  </button>

  {#if sslStatus?.cert_exists}
    <div style="margin-top: 16px; padding: 16px; background: var(--bg-elevated); border-radius: 8px; font-size: 13px; border-left: 3px solid var(--accent);">
      <p style="margin: 0 0 10px;"><strong>Setup steps (do this on EVERY device):</strong></p>
      <ol style="margin: 0; padding-left: 20px; line-height: 2;">
        <li><strong>Restart</strong> the GitInTheVan server</li>
        <li>Open <code>https://YOUR-IP:{location.port}</code> directly in the browser address bar</li>
        <li>Click <strong>Advanced</strong> &rarr; <strong>Accept the Risk</strong> (or "Proceed to site")<br>
          <span style="color: var(--text-dim); font-size: 11px;">This step is mandatory. Browsers silently block requests to untrusted certs without showing a warning for background requests (like JanitorAI's API calls).</span></li>
        <li>In JanitorAI, set the reverse proxy URL to:<br>
          <code>https://YOUR-IP:{location.port}/v1/chat/completions</code></li>
      </ol>
    </div>
  {/if}
</div>
