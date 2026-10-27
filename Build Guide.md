Text-to-SQL Interface with Guardrails


What You’re Building: 

A natural language interface that translates plain English questions into
SQL queries against a real database, executes them safely with guardrails preventing destructive
operations, validates that the generated SQL actually answers the question asked, and presents
results with a confidence score.



Build Guide
Phase 1: Build the Schema-Aware Prompt Engine 
1. Auto-extract the database schema: Use SQLAlchemy to introspect the database and
produce a structured representation: tables, columns with types, primary/foreign key
relationships, and sample values for categorical columns. This becomes the context the LLM
uses to write SQL.

2. Build the dynamic prompt constructor: For each user question, assemble a prompt with:
the relevant schema (not the entire database — filter to tables likely needed), foreign key
relationships, sample values for disambiguation, and any column descriptions or business
glossary terms. Include 3–5 few-shot examples of question-to-SQL pairs specific to this
schema.

3. Implement schema filtering: For large databases, sending the entire schema wastes
context and confuses the model. Build a lightweight relevance filter: embed the user
question, embed table/column descriptions, and include only tables above a similarity
threshold. This keeps the prompt focused and improves generation accuracy.

4. Handle ambiguity explicitly: When the user’s question maps to multiple possible
interpretations (e.g., “revenue” could mean gross or net), return a structured clarification
request instead of guessing. List the interpretations with example queries for each. This is a
production feature most demos skip.

Phase 2: Build the SQL Generation and Safety Layer 

1. Generate SQL with structured output: Use the instructor library or function calling to ensure
the LLM returns: the SQL query, a natural language explanation of what it does, a confidence
score, and a list of tables and columns accessed. Validate the SQL syntax before execution
using sqlparse.

2. Implement the guardrail middleware: Before any query executes, pass it through a safety
layer that: blocks all DDL (CREATE, ALTER, DROP), blocks all DML writes (INSERT,
UPDATE, DELETE), enforces a row limit (LIMIT 1000 if none specified), rejects queries with
subqueries deeper than 3 levels, and blocks queries estimated to scan more than N rows
using EXPLAIN. Make each rule configurable and log every blocked query with the reason.

3. Add query sandboxing: Execute all generated queries in a read-only transaction that rolls
back automatically. Use a database user with SELECT-only permissions as a second layer of
defense. Even if the guardrail layer misses something, the database permissions prevent
damage.

4. Build the execution layer: Run the validated SQL, capture results as a DataFrame, and
package the response with: raw results (capped at the row limit), execution time, rows
returned, and the EXPLAIN plan. Log everything for auditability.

Phase 3: Build the Hallucination Detection System 

1. Implement SQL-to-question verification: After generating the SQL, send the query back to
the LLM with the prompt: “What question does this SQL query answer?” Compare the back-
translated question to the original. If they diverge significantly, the SQL probably doesn’t
answer the right question. Score the alignment and flag low-confidence translations.

2. Add result sanity checking: After execution, perform basic sanity checks: are aggregated
values within plausible ranges? Do counts match expected magnitudes? Are date ranges
within the data’s timespan? Are there NULL-heavy columns that might indicate a bad JOIN?
Flag anomalies with specific explanations.

3. Build the multi-query validation: For complex questions, generate two different SQL
approaches independently (e.g., using different JOIN strategies or aggregation methods).
Execute both. If results match, confidence is high. If they diverge, flag the discrepancy and
present both results with explanations. Agreement between independent approaches is a
strong correctness signal.

4. Create a confidence scoring system: Combine signals into a single confidence score: SQL
syntax validity, back-translation alignment, result sanity check pass rate, multi-query
agreement, and schema coverage (did the query use the tables/columns you’d expect for this
question type?). Display confidence prominently alongside every result.

Phase 4: Build the Query Interface 

1. Build the API endpoints: POST /v1/query accepts a natural language question and returns:
the generated SQL, execution results, confidence score, and any guardrail warnings. GET
/v1/schema returns the database schema. GET /v1/history returns past queries and results
for the session.

2. Create a Streamlit or React frontend: A clean interface with: a text input for natural
language questions, the generated SQL displayed with syntax highlighting (editable for power
users), results in a sortable data table, the confidence score with a breakdown of contributing
signals, and a history panel showing past queries.

3. Add a feedback loop: Let users mark results as correct or incorrect. Store this feedback
alongside the query. Incorrect results become new test cases for the eval suite. Correct
results become new few-shot examples that improve future generation. This is the flywheel.

Phase 5: Build the Evaluation Suite 

1. Create a golden query dataset: Write 50+ natural language questions with verified correct
SQL and expected results. Include: simple lookups, multi-table JOINs, aggregations with
GROUP BY, date range filters, questions with ambiguous phrasing, and questions the
database cannot answer. This is your regression suite.

2. Run automated evals: For each test case, measure: SQL exact match (does the generated
SQL match the golden query?), execution match (do results match regardless of SQL
approach?), hallucination detection rate (does the system correctly flag bad queries?), and
guardrail effectiveness (are dangerous queries blocked?).

3. Containerize and document: Docker-compose with: PostgreSQL seeded with sample data,
the FastAPI service, and the frontend. Include a README that leads with the eval numbers:
“X% execution accuracy, Y% hallucination detection rate, zero unsafe queries executed
across Z test cases.”

Phase 6: Polish for Portfolio 

1. Record the demo: Show: a natural language question being translated to SQL, the guardrail
blocking a dangerous query, the hallucination detector catching a bad translation, and the
multi-query validation resolving a discrepancy. Under 4 minutes.

2. Write the narrative: Frame it as: “I built a text-to-SQL system with a X% accuracy rate that
blocks 100% of destructive operations and detects Y% of hallucinated queries before they
reach the user.” Lead with safety. Companies care more about not breaking things than about
accuracy percentages.