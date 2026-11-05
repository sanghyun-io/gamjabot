"""API routes"""
from fastapi import APIRouter
from app.api.routes import agents, projects, conversations, auth, remote, connections, provider_auth, dashboard, room_index, attention, agent_workload, readiness, starter, tools, workspace
from app.api.routes import room_results, room_memory

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["Authentication"])
api_router.include_router(agents.router, prefix="/agents", tags=["Agents"])
api_router.include_router(connections.router, prefix="/connections", tags=["Connections"])
api_router.include_router(provider_auth.router, prefix="/connections", tags=["Connections"])
api_router.include_router(tools.router, prefix="/tools", tags=["Tools"])
api_router.include_router(dashboard.router, prefix="/dashboard", tags=["Dashboard"])
api_router.include_router(room_index.router, prefix="/rooms", tags=["Rooms"])
api_router.include_router(attention.router, prefix="/attention", tags=["Attention"])
api_router.include_router(agent_workload.router, prefix="/agent-workloads", tags=["Agents"])
api_router.include_router(readiness.router, prefix="/readiness", tags=["Connections"])
api_router.include_router(starter.router, prefix="/starter", tags=["Starter"])
api_router.include_router(projects.router, prefix="/projects", tags=["Projects"])
api_router.include_router(workspace.router, prefix="/workspace", tags=["Workspace"])
api_router.include_router(conversations.router, prefix="/conversations", tags=["Conversations"])
api_router.include_router(room_results.router, prefix="/conversations", tags=["Room results"])
api_router.include_router(remote.router, prefix="/remote", tags=["Remote"])

api_router.include_router(room_memory.router, prefix="/conversations", tags=["Room memory"])

from app.api.routes import message_reactions
api_router.include_router(message_reactions.router, prefix="/conversations", tags=["Message reactions"])

from app.api.routes import room_plans
api_router.include_router(room_plans.router,prefix="/conversations",tags=["Room goals and watches"])

from app.api.routes import teammates
api_router.include_router(teammates.router, prefix='/conversations', tags=['Teammates'])

from app.api.routes import usage
api_router.include_router(usage.router, prefix='/usage', tags=['Usage'])

from app.api.routes import bot_profile, bot_memory
api_router.include_router(bot_profile.router, prefix="/bots", tags=["Personal Bots"])
api_router.include_router(bot_memory.router, prefix="/bots", tags=["Bot memory"])

from app.api.routes import bot_messages
api_router.include_router(bot_messages.router, prefix="/bots", tags=["Bot messages"])

from app.api.routes import owner_skills
api_router.include_router(owner_skills.router, tags=["Owner skills"])

from app.api.routes import owner_routines
api_router.include_router(owner_routines.router, tags=["Owner routines"])

from app.api.routes import mcp
api_router.include_router(mcp.router, tags=["MCP"])

from app.api.routes import library_uploads
api_router.include_router(library_uploads.router, prefix='/bots', tags=['Bot uploads'])

from app.api.routes import library_file_questions
api_router.include_router(library_file_questions.router, prefix='/conversations', tags=['File questions'])

from app.api.routes import bot_directory
api_router.include_router(bot_directory.router, prefix='/bot-directory', tags=['Bot directory'])

from app.api.routes import bot_clone
api_router.include_router(bot_clone.router, prefix='/bots', tags=['Bot copy'])

from app.api.routes import bot_templates
api_router.include_router(bot_templates.router, prefix='/bot-templates', tags=['Bot templates'])

from app.api.routes import template_publications
api_router.include_router(template_publications.router, prefix='/template-publications', tags=['Template publications'])

from app.api.routes import vault
api_router.include_router(vault.router, prefix='/vault', tags=['Vault'])

from app.api.routes import vault_contacts
api_router.include_router(vault_contacts.router, prefix='/vault', tags=['Vault contacts'])

from app.api.routes import vault_cards
api_router.include_router(vault_cards.router, prefix='/vault', tags=['Vault cards'])

from app.api.routes import voice_calls
api_router.include_router(voice_calls.router, prefix="/conversations", tags=["Voice calls"])

from app.api.routes import shared_bot_foundation
api_router.include_router(shared_bot_foundation.router, tags=['Team sharing setup'])

from app.api.routes import team_connection
api_router.include_router(team_connection.router, tags=['Team connection'])

from app.api.routes import shared_text_runtime
api_router.include_router(shared_text_runtime.router, tags=['Shared Bot text rooms'])

from app.api.routes import shared_connector
api_router.include_router(shared_connector.router, tags=['Shared personal connectors'])

from app.api.routes import slack
api_router.include_router(slack.router, tags=['Slack'])

from app.api.routes import local_notification
api_router.include_router(local_notification.router, tags=['Local notifications'])

from app.api.routes import page_xlsx
api_router.include_router(page_xlsx.router, prefix='/conversations', tags=['Page XLSX'])

from app.api.routes import skill_input
api_router.include_router(skill_input.router, prefix="/conversations", tags=["Skill input"])
