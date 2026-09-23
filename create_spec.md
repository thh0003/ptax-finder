# NFR
- Going to be host on AWS:
    - Container run on ECS Fargate
    - Fronted by AWS Application Loadbalancer
    - DB: Aurora Serverless
    - Frontend: React Vite
    - Backend: Node or Python you choose
    - Deployemnt via CDK

# Goal: Create an application which loads several years of a Counties Satellite Land Plots and compares the current years plots to a selected previous years and identifies plots with new structures on the land. So County Government can reassess the value of the plots with new structures.