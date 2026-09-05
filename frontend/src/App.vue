<template>
  <div>
    <div class="header">
      <h1>LLM API Gateway</h1>
      <div class="user-info">{{ userInfo }}</div>
    </div>

    <div v-if="!authChecked" class="container">
      <div class="readonly-notice">正在验证权限...</div>
    </div>

    <div v-else-if="!isAdmin" class="container">
      <div class="readonly-notice">
        <p>当前用户 <strong>{{ domainAccount }}</strong> 不是管理员</p>
        <p style="margin-top:8px;font-size:13px">请联系管理员将您的域账号添加到管理员名单</p>
      </div>
    </div>

    <div v-else class="container">
      <StatsGrid :status="status" @refresh="loadStatus" />

      <div class="tabs">
        <div v-for="t in tabs" :key="t.id"
             class="tab" :class="{ active: activeTab === t.id }"
             @click="activeTab = t.id">{{ t.label }}</div>
      </div>

      <div class="tab-panel" :class="{ active: activeTab === 'accounts' }">
        <AccountsPanel />
      </div>
      <div class="tab-panel" :class="{ active: activeTab === 'keys' }">
        <KeysPanel />
      </div>
      <div class="tab-panel" :class="{ active: activeTab === 'tenants' }">
        <TenantsPanel />
      </div>
      <div class="tab-panel" :class="{ active: activeTab === 'admins' }">
        <AdminsPanel />
      </div>
      <div class="tab-panel" :class="{ active: activeTab === 'token-usage' }">
        <TokenUsagePanel />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from './api.js'
import StatsGrid from './components/StatsGrid.vue'
import AccountsPanel from './components/AccountsPanel.vue'
import KeysPanel from './components/KeysPanel.vue'
import TenantsPanel from './components/TenantsPanel.vue'
import AdminsPanel from './components/AdminsPanel.vue'
import TokenUsagePanel from './components/TokenUsagePanel.vue'

const userInfo = ref('Loading...')
const domainAccount = ref('')
const isAdmin = ref(false)
const authChecked = ref(false)
const activeTab = ref('accounts')
const status = ref(null)

const tabs = [
  { id: 'accounts', label: '账号管理' },
  { id: 'keys', label: 'API Key' },
  { id: 'tenants', label: '租户管理' },
  { id: 'admins', label: '管理员' },
  { id: 'token-usage', label: 'Token 用量' },
]

async function loadStatus() {
  status.value = await api('/admin/status')
}

async function checkAuth() {
  const info = await api('/admin/auth/check')
  domainAccount.value = info.domain_account
  userInfo.value = info.domain_account + ' ' + (info.is_admin ? '(管理员)' : '(普通用户)')
  isAdmin.value = info.is_admin
  authChecked.value = true
  if (info.is_admin) loadStatus()
}

onMounted(checkAuth)
</script>
