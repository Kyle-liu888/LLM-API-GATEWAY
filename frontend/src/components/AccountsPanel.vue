<template>
  <div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h3 style="margin:0">上游账号（Copilot）</h3>
      <div>
        <button class="btn" @click="resetCounts">重置计数</button>
        <button class="btn btn-primary" @click="load">刷新</button>
      </div>
    </div>
    <table>
      <thead><tr><th>账号ID</th><th>显示名</th><th>启用</th><th>健康</th><th>在飞</th><th>月总请求</th><th>日Token</th><th>月Token</th><th>年Token</th></tr></thead>
      <tbody>
        <tr v-for="a in accounts" :key="a.account_id">
          <td>{{ a.account_id }}</td>
          <td>{{ a.display_name }}</td>
          <td><span class="badge" :class="a.is_active ? 'badge-active' : 'badge-revoked'">{{ a.is_active ? '启用' : '禁用' }}</span></td>
          <td><span class="badge" :class="'badge-' + a.status">{{ a.status }}</span></td>
          <td>{{ a.active_count }}</td>
          <td>{{ a.total_requests }}</td>
          <td>{{ fmtTokens(a.daily_tokens) }}</td>
          <td>{{ fmtTokens(a.monthly_tokens) }}</td>
          <td>{{ fmtTokens(a.yearly_tokens) }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api, fmtTokens } from '../api.js'

const accounts = ref([])

async function load() {
  accounts.value = await api('/admin/accounts')
}

async function resetCounts() {
  if (!confirm('确定重置所有账号的 active_count？')) return
  await api('/admin/accounts/reset-counts', { method: 'POST' })
  await load()
}

onMounted(load)
</script>
