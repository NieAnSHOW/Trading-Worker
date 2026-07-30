<template>
  <main class="console settings-page">
    <section aria-labelledby="service-config-title">
      <h1 id="service-config-title">服务配置</h1>
      <p>导入包含用户 API 地址的桌面配置 JSON 文件。</p>
      <label for="service-config-file">配置文件</label>
      <input
        id="service-config-file"
        data-test="service-config-file"
        type="file"
        accept="application/json,.json"
        :disabled="importing"
        @change="importConfig"
      />
      <p v-if="userApiUrl" role="status">已导入服务地址：{{ userApiUrl }}</p>
      <p v-if="error" role="alert">{{ error }}</p>
    </section>
  </main>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { consoleImportUserApiConfig } from "../ipc/commands";

const importing = ref(false);
const userApiUrl = ref("");
const error = ref("");

async function importConfig(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;

  input.value = "";
  importing.value = true;
  error.value = "";
  try {
    const result = await consoleImportUserApiConfig(await file.text());
    userApiUrl.value = result.userApiUrl;
  } catch {
    error.value = "无法导入配置。请确认所选 JSON 文件有效后重试。";
  } finally {
    importing.value = false;
  }
}
</script>
