Part 1: What in my neighborhood does the city directly impact? 
Building: Chicago City Services AI Front Door

This is a very open-ended question, so I started with the source: The City of Chicago website, chicago.gov. The City lists all of its departments & what they do on their Departments page. This list is long. Some information is relevant, but some is not. I also had no idea if this was the right place to start. 

Interestingly, two services I was looking for were missing: 
Park facilities 
Chicago Public Schools enrollment information 
Turns out, these are not managed by the City of Chicago, but are separate government entities with their own websites (chicagoparkdistrict.com , cps.edu). (Also turns out, Chicago has a TV channel…?)  

User (aka me): It would be really great, if instead of having to know where to look & what sources to trust, there was a triage tool that would tell me where to look. What a great application of an AI Front Door.

What is an AI Front Door? 
An AI Front Door is a very trendy concept right now, but all it is is an interactive directory chatbot that directs users to the right place. Instead of having to know (or sorting through Google) that Chicago Business Direct is the right place to apply for a business license, a Chicago services front door would allow you to ask whatever you want - “Where do I apply for a business license in Chicago?”, and it would direct you to the right place. 

I built out a prototype City of Chicago AI Front Door here. Check it out & let me know what you think! 

Note: This tool runs on a limited snapshot of scraped city data. It is not comprehensive & could provide out of date information. Treat this as a prototype only & please verify the information provided.

Build notes: 
In testing out my chatbot, I found that the main benefit of this tool came from its user flow & its flexibility. The structure of a chatbot more closely matches how users search for information. They come with a specific question, but they don’t know what department handles their issue or what sites are official. For example, chicago.gov is a *.gov website, but chicagoparkdistrict.com is a *.com website. I would have expected all sites to end in *.gov if it was the official government sanctioned site. An interactive front door would limit the cognitive load on the user in having to sift through all this information.

The chatbot could also easily support multiple languages, and provide information at different reading grade levels (I set it to 5th grade as per standard). The main benefit of a “front door” chatbot that I found is that it more closely mimics how users look for information, and can better support users of all types. 

The main limitations of a tool like this is 1) building a comprehensive database of up to date & accurate information about city services - including separate entities like CPS & the Parks District, and 2) it requires constant monitoring to make sure that the answers the chatbot is providing is accurate. For the city, 1) is likely easier for them than for me, and 2) is likely easier for me than for them. 

I implemented the chatbot using Retrieval Augmented Generation (RAG), which means the chatbot does not have access to the internet, and only has access to the information in a local database. This ensures something like the Chipotle chatbot incident doesn’t happen, but requires that the database of information it refers to is up to date. I found the data to surface in the chatbot by scraping the City of Chicago, Chicago Parks District, and Chicago Public Schools websites, but since they’re publishing the websites, they should have the comprehensive data already in their backend to build the chatbot on top of. I implemented this tool using a shallow crawl (only crawling the site 1 link deep from the homepage) in order to limit the amount of memory this took up on my already limited-memory laptop. 

To monitor the chatbot’s responses, and to audit them, I implemented anonymous logging of the conversations. This will be critical for future analytics - helping understand what questions users are asking, audit the answers being given, and understand how city services can be improved.  

In building the chatbot, the tool did hallucinate a lot, and I had to add guardrails to its instructions - like always citing sources, or saying “I don’t know” if the information is not directly in the context provided. I found the most useful refinement strategy for building out the chatbot was just using it. Initial test conversations resulted in too many clarifying questions, so I limited the number of consecutive clarifications the chatbot would ask for. I found that the chatbot kept returning the Privacy Policy page as a source & realized this is because the link appeared on every scraped page so it was given high relevance for every answer. I refined the tool by removing links that appeared on every scraped page so page information could be more highly differentiated. There’s definitely more edge cases that haven’t been caught and will only be caught with user activity. 

Advice for the city: 
In building a tool like this, here are my recommendations on how to have impact while limiting risk: 
Limit the chatbot’s knowledge base. Use only data from official city sources that are actively monitored & kept up to date 
Add traceability - The chatbot’s answer should be traceable to an official government source both for user trust but also for auditability for tool QA.
Test test test - if this is an official government service, it is critical that it is well-tested. 
A recommended roll-out plan is to test first with internal city employees, then a limited group of savvy users, before rolling it out to the general public. 
Create quality metrics (like % response rate accuracy), and monitor them continuously 
Have clear user instructions - Be very clear to the user on what to use & not use the tool for. Users should be clear on benefits & limitations of the tool, what to trust it for and not trust it for. 
When in doubt, point the user to the source data. Don’t rely solely on the chatbot to answer all users’ questions. Use it more as a triage tool than an expert. 

What’s next: 
In this process, I answered (at least for now) my question of - what does the City do?

To get feedback on this from a city perspective, I spent a while trying to figure out who in the city would be the right person to contact about this chatbot. I could not figure out who it would be. The Chicago Department of Technology & Innovation has a Contact Us section, but it goes to: https://cityofchicago.service-now.com/itsp . So for now, I’ll just contact my local alderman & see what he says.

As I learned more about city services, I also had more complex questions like - not only how do I apply for a city business license, but - in my neighborhood, how many business licenses have been approved? These types of questions are not available via the city’s public websites, but can be answered using the City of Chicago Data Portal. Next is exploring what data is available & how easily it answers these next set of questions. 


Did you find this interesting? Let me know what you think here. 
