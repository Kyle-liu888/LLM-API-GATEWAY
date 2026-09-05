<template>
  <div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h3 style="margin:0">管理员名单</h3>
      <button class="btn btn-primary" @click="showAdd">添加管理员</button>
    </div>
    <table>
      <thead><tr><th>域账号</th><th>显示名</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="a in admins" :key="a.domain_account">
          <td>{{ a.domain_account }}</td>
          <td>{{ a.display_name }}</td>
          <td><button class="btn btn-danger" @click="doRemove(a.domain_account)">移除</button></td>
        </tr>
      </tbody>
    </table>

    <Modal :visible="addVisible" title="添加管理员" @close="addVisible = false">
      <div class="form-group"><label>域账号</label><input v-model="fAccount" placeholder="如：demo_admin"></div>
      <div class="form-group"><label>显示名</label><input v-model="fDisplayName" placeholder="如：演示管理员"></div>
      <template #actions>
        <button class="btn" @click="addVisible = false">取消</button>
        <button class="btn btn-primary" @click="doAdd">添加</button>
      </template>
    </Modal>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'
import Modal from './Modal.vue'

const admins = ref([])
const addVisible = ref(false)
const fAccount = ref('')
const fDisplayName = ref('')

async function load() {
  admins.value = await api('/admin/admins')
}

function showAdd() {
  fAccount.value = ''
  fDisplayName.value = ''
  addVisible.value = true
}

async function doAdd() {
  await api('/admin/admins', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ domain_account: fAccount.value, display_name: fDisplayName.value })
  })
  addVisible.value = false
  await load()
}

async function doRemove(account) {
  if (!confirm('确定移除管理员 ' + account + '？')) return
  await api('/admin/admins/' + account, { method: 'DELETE' })
  await load()
}

onMounted(load)
</script>
