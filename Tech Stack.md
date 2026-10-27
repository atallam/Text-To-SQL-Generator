Tech Stack

Component Tool / Library Why This Choice
Language 			Python 3.11+ 
LLM Claude 			Sonnet 5.5 
Database 			SQL (Supabase)  
Schema Extraction 	SQLAlchemy Automatic schema introspection
Guardrails 			Custom middleware Prevents destructive queries
Validation LLM-as-judge + result check Hallucination detection
API 				FastAPI Production-grade serving
Containerization Docker + docker-compose DB + API orchestration
