<template>
  <div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h3 style="margin:0">API Key 管理</h3>
      <button class="btn btn-primary" @click="showCreate">创建 Key</button>
    </div>
    <table>
      <thead><tr><th>Key</th><th>租户</th><th>名称</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="k in keys" :key="k.key_value">
          <td style="font-family:monospace">{{ k.key_display }}</td>
          <td>{{ k.tenant_id }}</td>
          <td>{{ k.name || '-' }}</td>
          <td><span class="badge" :class="k.is_active ? 'badge-active' : 'badge-revoked'">{{ k.is_active ? '活跃' : '已吊销' }}</span></td>
          <td v-if="k.is_active">
            <button class="btn" @click="doRotate(k.key_value)">轮换</button>
            <button class="btn btn-danger" @click="doRevoke(k.key_value)">吊销</button>
          </td>
          <td v-else></td>
        </tr>
      </tbody>
    </table>

    <Modal :visible="modalVisible" :title="modalTitle" @close="modalVisible = false">
      <template v-if="!newKey">
        <div class="form-group"><label>租户</label>
          <select v-model="formTenant">
            <option v-for="t in tenants" :key="t.tenant_id" :value="t.tenant_id">{{ t.name }} ({{ t.tenant_id }})</option>
          </select>
        </div>
        <div class="form-group"><label>名称（可选）</label><input v-model="formName" placeholder="如：dev-key"></div>
      </template>
      <template #actions>
        <button class="btn" @click="modalVisible = false">取消</button>
        <button class="btn btn-primary" @click="doCreate" v-if="!newKey">创建</button>
        <button class="btn btn-success" @click="modalVisible = false" v-else>确认</button>
      </template>
      <template #extra>
        <div class="new-key-box" v-if="newKey">新 Key: {{ newKey }}  (请立即复制，仅显示一次)</div>
      </template>
    </Modal>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'
import Modal from './Modal.vue'

const keys = ref([])
const tenants = ref([])
const modalVisible = ref(false)
const modalTitle = ref('创建 API Key')
const formTenant = ref('')
const formName = ref('')
const newKey = ref('')

async function load() {
  keys.value = await api('/admin/keys')
}

async function showCreate() {
  tenants.value = await api('/admin/tenants')
  formTenant.value = tenants.value[0]?.tenant_id || ''
  formName.value = ''
  newKey.value = ''
  modalTitle.value = '创建 API Key'
  modalVisible.value = true
}

async function doCreate() {
  const result = await api('/admin/keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tenant_id: formTenant.value, name: formName.value })
  })
  newKey.value = result.key_value
  await load()
}

async function doRevoke(keyValue) {
  if (!confirm('确定吊销此 Key？吊销后不可恢复。')) return
  await api('/admin/keys/' + encodeURIComponent(keyValue), { method: 'DELETE' })
  await load()
}

async function doRotate(keyValue) {
  if (!confirm('确定轮换此 Key？旧 Key 将被吊销，新 Key 将生成。')) return
  const result = await api('/admin/keys/' + encodeURIComponent(keyValue) + '/rotate', { method: 'POST' })
  newKey.value = result.key_value
  modalTitle.value = '轮换 Key'
  tenants.value = await api('/admin/tenants')
  modalVisible.value = true
  await load()
}

onMounted(load)
</script>
