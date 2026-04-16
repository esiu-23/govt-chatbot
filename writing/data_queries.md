MCP v. API v. RAG in the context of LLMs
Let’s unpack some 3 letter acronyms

In my last post, I created a prototype for a Chicago City Services AI Front Door. I found that I could create a user-friendly interface that triaged simple questions about city services and directed the user to the right web page to answer their questions.

[Link to www.thegovernmentandme.tools]

I left off with a follow-up question of - what if I not only want to know how to apply for a city business license, but how many business licenses have been granted in my neighborhood? 
Luckily, Chicago has a well-maintained Open Data Portal that contains information like this.

The data
The Chicago Open Data Portal was created in 2012 via an Executive Order signed by Mayor Rahm Emanuel. The federal government followed as well, with a bipartisan 2018 law that created the federal open data portal, at https://data.gov/open-gov/. As of this week, there are 131 Open Data portals by cities & states in the US. Open data portals like this are indispensable to government accountability and evidence-based policymaking. It is critical we make sure they remain comprehensive, up to date, and free. 

So a user wanted to know how many business licenses were approved in their neighborhood, they have a few options: 
Google their question & read the AI generated answer 
Go to the Chicago Data Portal & use the tools in the browser to visualize, query, and examine the business licenses data: https://data.cityofchicago.org/d/r5kz-chrr/visualization 
Download the raw data & use Excel to manipulate / visualize the data 
Write a script to call the business licenses dataset endpoint & visualize the results 

Most users are likely going to do Option 1. This is the most user-friendly, but the least controllable/reliable data source. This is where the city has an opportunity to provide a similarly user-friendly interface for querying data, but with trusted data sources. 

This is a hot topic right now. Recently Nathan Storey (NYC Office of Technology & Innovation) built https://www.civicaitools.org/ , which allows users to ask data questions in plain English, and the tool will live query NYC’s Open Data portal & respond to the user with the answer to their question using real-time data. The way this tool works is that it uses a Model Context Protocol (MCP). 

[Some content on how I implemented Nathan Storey’s code] 





The implementation

What is a Model Context Protocol (MCP)? 
To explain, let’s go back to our original question - how many business licenses were approved in my neighborhood in the past year? 

Today: If you want the raw data today on business licenses from Chicago Open Data Portal, you have 2 options - download the raw data as a CSV or “download” the raw data via API. In both cases, you have to go through a series of transformations: 
Your raw question - how many business licenses were approved in my neighborhood? 
Search for the right dataset - find the right dataset ID in Chicago Open Data Portal 
Work with the raw data to answer your question - Understand the data structure & manipulate the data appropriately 

Using a MCP: With a MCP, you are using a LLM (large language model) to do all of those steps for you. The model translates your plain english question into key words to search for the right dataset in the Chicago Open Data Portal, finds what it thinks is the right data, downloads it, and manipulates the raw data to answer your question. 

[ what is a MCP doing behind the scenes? ]


What are the pros & cons of using a MCP? 
Honestly, MCPs are pretty great. In my opinion, the main risks of using a MCP is 1) traceability, and 2) cybersecurity. 

But you also often have no idea how they’re getting their answers. If the query returned 25 versus 500 business licenses approved last year, I’d have no idea if that number was right or not. The main downside of using a MCP in my opinion (aside from cybersecurity risk) is the lack of traceability. 

3 letter acronym alert: 
CSV: Comma-Separated Values. A file containing values separated by commas. Traditionally used in Excel.
API: Application Programming Interface. Basically a raw-download of data as well, but via a different interface that requires programming knowledge.
LLM: Large Language Model. 
MCP: Model Context Protocol. Allows a xxxx 
RAG: Retrieval Augmented Generation. Xxxx 

So should we use a MCP? 
To me, a key attribute of Open Data requests is traceability. As an intermediary between the published data & the user themselves, it’s critical to make sure 1) we’re translating the user’s intent correctly, and 2) we’re using the data correctly. As with the Chicago City Services AI Front Door, I think of these tools more as a triage for the user pointing them to the right direction than outright being an expert answering their questions. 
