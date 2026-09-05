<template>
  <div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h3 style="margin:0">Token 用量</h3>
      <div style="display:flex;gap:8px;align-items:center">
        <input v-model="searchText" placeholder="搜索账号..." style="padding:6px 10px;border:1px solid #ddd;border-radius:4px;font-size:13px;width:160px" />
        <label style="font-size:13px;font-weight:600;color:#555">模型</label>
        <select v-model="selectedModel" @change="load" style="padding:6px 10px;border:1px solid #ddd;border-radius:4px;font-size:13px">
          <option value="">所有模型</option>
          <option v-for="m in modelOptions" :key="m" :value="m">{{ m }}</option>
        </select>
        <button class="btn btn-primary" @click="load">刷新</button>
      </div>
    </div>
    <table>
      <thead><tr>
        <th>账号</th>
        <th>租户</th>
        <th>模型</th>
        <th class="sortable" @click="toggleSort('daily_tokens')">日Token <span v-html="sortIcon('daily_tokens')"></span></th>
        <th class="sortable" @click="toggleSort('monthly_tokens')">月Token <span v-html="sortIcon('monthly_tokens')"></span></th>
        <th class="sortable" @click="toggleSort('yearly_tokens')">年Token <span v-html="sortIcon('yearly_tokens')"></span></th>
      </tr></thead>
      <tbody>
        <tr v-for="r in displayRows" :key="r.user_account + r.tenant_id">
          <td>{{ r.user_account }}</td>
          <td>{{ r.tenant_name }}</td>
          <td>{{ selectedModel || '全部' }}</td>
          <td>{{ fmtTokens(r.daily_tokens) }}</td>
          <td>{{ fmtTokens(r.monthly_tokens) }}</td>
          <td>{{ fmtTokens(r.yearly_tokens) }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api, fmtTokens } from '../api.js'

const rows = ref([])
const selectedModel = ref('')
const modelOptions = ref([])
const searchText = ref('')
const sortKey = ref('')
const sortAsc = ref(true)

async function loadModelOptions() {
  const health = await api('/health')
  modelOptions.value = health.supported_models || []
}

async function load() {
  const url = selectedModel.value
    ? '/admin/token-usage?model=' + encodeURIComponent(selectedModel.value)
    : '/admin/token-usage'
  rows.value = await api(url)
}

function toggleSort(key) {
  if (sortKey.value === key) {
    sortAsc.value = !sortAsc.value
  } else {
    sortKey.value = key
    sortAsc.value = false
  }
}

function sortIcon(key) {
  if (sortKey.value !== key) return '&#9650;&#9660;'
  return sortAsc.value ? '&#9650;' : '&#9660;'
}

const displayRows = computed(() => {
  let result = rows.value
  if (searchText.value.trim()) {
    const q = searchText.value.trim().toLowerCase()
    result = result.filter(r => r.user_account.toLowerCase().includes(q))
  }
  if (sortKey.value) {
    const dir = sortAsc.value ? 1 : -1
    result = [...result].sort((a, b) => (a[sortKey.value] - b[sortKey.value]) * dir)
  }
  return result
})

onMounted(() => { loadModelOptions(); load() })
</script>

<style scoped>
.sortable { cursor: pointer; user-select: none; }
.sortable:hover { color: #1e78c8; }
</style>
