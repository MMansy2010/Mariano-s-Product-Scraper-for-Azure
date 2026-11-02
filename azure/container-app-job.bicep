@description('The Azure region where resources are deployed.')
param location string = resourceGroup().location

@description('The name of the Azure Container Apps Environment.')
param environmentName string = 'cae-marianos-scraper-prod'

@description('The name of the Container Apps Job.')
param jobName string = 'job-marianos-catalog-scraper'

@description('Container image URL with tag.')
param containerImage string = 'mcr.microsoft.com/playwright/python:v1.52.0-noble'

@description('Cron expression in UTC for Azure Container Apps Jobs (ACA does not support local timezone evaluation). Schedule "0 9 * * 2,5" executes Tuesdays and Fridays at 09:00 UTC, which corresponds to 03:00 AM Central Standard Time (CST, UTC-6 in winter) and 04:00 AM Central Daylight Time (CDT, UTC-5 in summer). If exact 03:00 AM local time year-round is required, either update the cron parameter semi-annually (0 8 * * 2,5 during CDT) or trigger the job via an Azure Logic App with recurrence timezone support.')
param cronExpression string = '0 9 * * 2,5'

@description('Target storage account name for catalog blob publication.')
param storageAccountName string

@secure()
@description('Connection string for the Azure Storage account.')
param storageConnectionString string

@secure()
@description('Kroger Developer Client ID.')
param krogerClientId string

@secure()
@description('Kroger Developer Client Secret.')
param krogerClientSecret string

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}

resource scraperJob 'Microsoft.App/jobs@2024-03-01' = {
  name: jobName
  location: location
  properties: {
    environmentId: environment.id
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: cronExpression
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaTimeout: 7200
      replicaRetryLimit: 1
      secrets: [
        {
          name: 'storage-connection-string'
          value: storageConnectionString
        }
        {
          name: 'kroger-client-id'
          value: krogerClientId
        }
        {
          name: 'kroger-client-secret'
          value: krogerClientSecret
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'collector'
          image: containerImage
          command: [
            'python'
            'src/kroger_collector.py'
            '--config'
            'config/settings.json'
            '--target-count'
            '500'
          ]
          env: [
            {
              name: 'AZURE_STORAGE_CONTAINER_NAME'
              value: 'marianos-catalog'
            }
            {
              name: 'AZURE_STORAGE_CONNECTION_STRING'
              secretRef: 'storage-connection-string'
            }
            {
              name: 'KROGER_CLIENT_ID'
              secretRef: 'kroger-client-id'
            }
            {
              name: 'KROGER_CLIENT_SECRET'
              secretRef: 'kroger-client-secret'
            }
            {
              name: 'TZ'
              value: 'America/Chicago'
            }
            {
              name: 'PYTHONUNBUFFERED'
              value: '1'
            }
          ]
          resources: {
            cpu: json('1.0')
            memory: '2.0Gi'
          }
        }
      ]
    }
  }
}

output jobResourceId string = scraperJob.id
output jobName string = scraperJob.name
