<template>
  <div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h3 style="margin:0">租户管理</h3>
      <button class="btn btn-primary" @click="showCreate">创建租户</button>
    </div>
    <table>
      <thead><tr><th>租户ID</th><th>名称</th><th>配额(请求数/每小时)</th><th>允许模型</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="t in tenants" :key="t.tenant_id">
          <td>{{ t.tenant_id }}</td>
          <td>{{ t.name }}</td>
          <td>{{ t.quota_limit === -1 ? '无限' : t.quota_limit + '/h' }}</td>
          <td>{{ t.allowed_models.length ? t.allowed_models.join(', ') : '全部' }}</td>
          <td>
            <button class="btn" @click="showEdit(t)">编辑</button>
            <button class="btn btn-danger" @click="doDelete(t.tenant_id)">删除</button>
          </td>
        </tr>
      </tbody>
    </table>

    <!-- Create Tenant Modal -->
    <Modal :visible="createVisible" title="创建租户" @close="createVisible = false">
      <div class="form-group"><label>名称</label><input v-model="cName" placeholder="如：Alpha Team"></div>
      <div class="form-group"><label>每小时请求配额（-1=无限）</label><input v-model.number="cQuota" type="number"></div>
      <div class="form-group"><label>允许模型（逗号分隔，空=全部）</label><input v-model="cModels" placeholder="如：Glm-5.1,MiniMax-M2.7"></div>
      <template #actions>
        <button class="btn" @click="createVisible = false">取消</button>
        <button class="btn btn-primary" @click="doCreate">创建</button>
      </template>
    </Modal>

    <!-- Edit Tenant Modal -->
    <Modal :visible="editVisible" title="编辑租户" @close="editVisible = false">
      <div class="form-group"><label>名称</label><input v-model="eName"></div>
      <div class="form-group"><label>每小时请求配额（-1=无限）</label><input v-model.number="eQuota" type="number"></div>
      <div class="form-group"><label>允许模型（逗号分隔，空=全部）</label><input v-model="eModels" placeholder="如：Glm-5.1,MiniMax-M2.7"></div>
      <template #actions>
        <button class="btn" @click="editVisible = false">取消</button>
        <button class="btn btn-primary" @click="doEdit">保存</button>
      </template>
    </Modal>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'
import Modal from './Modal.vue'

const tenants = ref([])
const createVisible = ref(false)
const editVisible = ref(false)

// Create form
const cName = ref('')
const cQuota = ref(-1)
const cModels = ref('')

// Edit form
const eId = ref('')
const eName = ref('')
const eQuota = ref(-1)
const eModels = ref('')

async function load() {
  tenants.value = await api('/admin/tenants')
}

function showCreate() {
  cName.value = ''
  cQuota.value = -1
  cModels.value = ''
  createVisible.value = true
}

async function doCreate() {
  const models = cModels.value.trim() ? cModels.value.trim().split(',').map(s => s.trim()) : null
  await api('/admin/tenants', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: cName.value, quota_limit: cQuota.value, allowed_models: models })
  })
  createVisible.value = false
  await load()
}

function showEdit(tenant) {
  eId.value = tenant.tenant_id
  eName.value = tenant.name
  eQuota.value = tenant.quota_limit
  eModels.value = tenant.allowed_models.join(',')
  editVisible.value = true
}

async function doEdit() {
  const models = eModels.value.trim() ? eModels.value.trim().split(',').map(s => s.trim()) : null
  await api('/admin/tenants/' + eId.value, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: eName.value, quota_limit: eQuota.value, allowed_models: models })
  })
  editVisible.value = false
  await load()
}

async function doDelete(tenantId) {
  if (!confirm('确定删除租户 "' + tenantId + '"？该租户下的所有 API Key 也将被删除，此操作不可恢复。')) return
  await api('/admin/tenants/' + tenantId, { method: 'DELETE' })
  await load()
}

onMounted(load)
</script>
